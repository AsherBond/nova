# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Quotas for resources per project."""

import copy

from oslo_log import log as logging
from oslo_utils import importutils
from sqlalchemy import sql

import nova.conf
from nova import context as nova_context
from nova.db.api import api as api_db_api
from nova.db.api import models as api_models
from nova.db.main import api as main_db_api
from nova import exception
from nova.limit import local as local_limit
from nova.limit import placement as placement_limit
from nova import objects
from nova.scheduler.client import report

LOG = logging.getLogger(__name__)
CONF = nova.conf.CONF
# Lazy-loaded on first access.
# Avoid constructing the KSA adapter and provider tree on every access.
PLACEMENT_CLIENT = None
# If user_id and queued_for_delete are populated for a project, cache the
# result to avoid doing unnecessary EXISTS database queries.
UID_QFD_POPULATED_CACHE_BY_PROJECT = set()
# For the server group members check, we do not scope to a project, so if all
# user_id and queued_for_delete are populated for all projects, cache the
# result to avoid doing unnecessary EXISTS database queries.
UID_QFD_POPULATED_CACHE_ALL = False


class DbQuotaDriver(object):
    """Driver to perform necessary checks to enforce quotas and obtain
    quota information.  The default driver utilizes the local
    database.
    """
    UNLIMITED_VALUE = -1

    def get_reserved(self):
        # Since we stopped reserving the DB, we just return 0
        return 0

    def get_defaults(self, context, resources):
        """Given a list of resources, retrieve the default quotas.
        Use the class quotas named `_DEFAULT_QUOTA_NAME` as default quotas,
        if it exists.

        :param context: The request context, for access checks.
        :param resources: A dictionary of the registered resources.
        """

        quotas = {}
        default_quotas = objects.Quotas.get_default_class(context)
        for resource in resources.values():
            # resource.default returns the config options. So if there's not
            # an entry for the resource in the default class, it uses the
            # config option.
            quotas[resource.name] = default_quotas.get(resource.name,
                                                       resource.default)

        return quotas

    def get_class_quotas(self, context, resources, quota_class):
        """Given a list of resources, retrieve the quotas for the given
        quota class.

        :param context: The request context, for access checks.
        :param resources: A dictionary of the registered resources.
        :param quota_class: The name of the quota class to return
                            quotas for.
        """

        quotas = {}
        class_quotas = objects.Quotas.get_all_class_by_name(context,
                                                            quota_class)
        for resource in resources.values():
            quotas[resource.name] = class_quotas.get(resource.name,
                                                     resource.default)

        return quotas

    def _process_quotas(self, context, resources, project_id, quotas,
                        quota_class=None, usages=None,
                        remains=False):
        modified_quotas = {}
        # Get the quotas for the appropriate class.  If the project ID
        # matches the one in the context, we use the quota_class from
        # the context, otherwise, we use the provided quota_class (if
        # any)
        if project_id == context.project_id:
            quota_class = context.quota_class
        if quota_class:
            class_quotas = objects.Quotas.get_all_class_by_name(context,
                                                                quota_class)
        else:
            class_quotas = {}

        default_quotas = self.get_defaults(context, resources)

        for resource in resources.values():
            limit = quotas.get(resource.name, class_quotas.get(
                        resource.name, default_quotas[resource.name]))
            modified_quotas[resource.name] = dict(limit=limit)

            # Include usages if desired.  This is optional because one
            # internal consumer of this interface wants to access the
            # usages directly from inside a transaction.
            if usages:
                usage = usages.get(resource.name, {})
                modified_quotas[resource.name].update(
                    in_use=usage.get('in_use', 0),
                    )

            # Initialize remains quotas with the default limits.
            if remains:
                modified_quotas[resource.name].update(remains=limit)

        if remains:
            # Get all user quotas for a project and subtract their limits
            # from the class limits to get the remains. For example, if the
            # class/default is 20 and there are two users each with quota of 5,
            # then there is quota of 10 left to give out.
            all_quotas = objects.Quotas.get_all(context, project_id)
            for quota in all_quotas:
                if quota.resource in modified_quotas:
                    modified_quotas[quota.resource]['remains'] -= \
                            quota.hard_limit

        return modified_quotas

    def _get_usages(self, context, resources, project_id, user_id=None):
        """Get usages of specified resources.

        This function is called to get resource usages for validating quota
        limit creates or updates in the os-quota-sets API and for displaying
        resource usages in the os-used-limits API. This function is not used
        for checking resource usage against quota limits.

        :param context: The request context for access checks
        :param resources: The dict of Resources for which to get usages
        :param project_id: The project_id for scoping the usage count
        :param user_id: Optional user_id for scoping the usage count
        :returns: A dict containing resources and their usage information,
                  for example:
                  {'project_id': 'project-uuid',
                   'user_id': 'user-uuid',
                   'instances': {'in_use': 5}}
        """
        usages = {}
        for resource in resources.values():
            # NOTE(melwitt): We should skip resources that are not countable,
            # such as AbsoluteResources.
            if not isinstance(resource, CountableResource):
                continue
            if resource.name in usages:
                # This is needed because for any of the resources:
                # ('instances', 'cores', 'ram'), they are counted at the same
                # time for efficiency (query the instances table once instead
                # of multiple times). So, a count of any one of them contains
                # counts for the others and we can avoid re-counting things.
                continue
            if resource.name in ('key_pairs', 'server_group_members'):
                # These per user resources are special cases whose usages
                # are not considered when validating limit create/update or
                # displaying used limits. They are always zero.
                usages[resource.name] = {'in_use': 0}
            else:
                if (
                    resource.name in
                    main_db_api.quota_get_per_project_resources()
                ):
                    count = resource.count_as_dict(context, project_id)
                    key = 'project'
                else:
                    # NOTE(melwitt): This assumes a specific signature for
                    # count_as_dict(). Usages used to be records in the
                    # database but now we are counting resources. The
                    # count_as_dict() function signature needs to match this
                    # call, else it should get a conditional in this function.
                    count = resource.count_as_dict(context, project_id,
                                                   user_id=user_id)
                    key = 'user' if user_id else 'project'
                # Example count_as_dict() return value:
                #   {'project': {'instances': 5},
                #    'user': {'instances': 2}}
                counted_resources = count[key].keys()
                for res in counted_resources:
                    count_value = count[key][res]
                    usages[res] = {'in_use': count_value}
        return usages

    def get_user_quotas(self, context, resources, project_id, user_id,
                        quota_class=None,
                        usages=True, project_quotas=None,
                        user_quotas=None):
        """Given a list of resources, retrieve the quotas for the given
        user and project.

        :param context: The request context, for access checks.
        :param resources: A dictionary of the registered resources.
        :param project_id: The ID of the project to return quotas for.
        :param user_id: The ID of the user to return quotas for.
        :param quota_class: If project_id != context.project_id, the
                            quota class cannot be determined.  This
                            parameter allows it to be specified.  It
                            will be ignored if project_id ==
                            context.project_id.
        :param usages: If True, the current counts will also be returned.
        :param project_quotas: Quotas dictionary for the specified project.
        :param user_quotas: Quotas dictionary for the specified project
                            and user.
        """
        if user_quotas:
            user_quotas = user_quotas.copy()
        else:
            user_quotas = objects.Quotas.get_all_by_project_and_user(
                context, project_id, user_id)
        # Use the project quota for default user quota.
        proj_quotas = project_quotas or objects.Quotas.get_all_by_project(
            context, project_id)
        for key, value in proj_quotas.items():
            if key not in user_quotas.keys():
                user_quotas[key] = value
        user_usages = {}
        if usages:
            user_usages = self._get_usages(context, resources, project_id,
                                           user_id=user_id)
        return self._process_quotas(context, resources, project_id,
                                    user_quotas, quota_class,
                                    usages=user_usages)

    def get_project_quotas(self, context, resources, project_id,
                           quota_class=None,
                           usages=True, remains=False, project_quotas=None):
        """Given a list of resources, retrieve the quotas for the given
        project.

        :param context: The request context, for access checks.
        :param resources: A dictionary of the registered resources.
        :param project_id: The ID of the project to return quotas for.
        :param quota_class: If project_id != context.project_id, the
                            quota class cannot be determined.  This
                            parameter allows it to be specified.  It
                            will be ignored if project_id ==
                            context.project_id.
        :param usages: If True, the current counts will also be returned.
        :param remains: If True, the current remains of the project will
                        will be returned.
        :param project_quotas: Quotas dictionary for the specified project.
        """
        project_quotas = project_quotas or objects.Quotas.get_all_by_project(
            context, project_id)
        project_usages = {}
        if usages:
            project_usages = self._get_usages(context, resources, project_id)
        return self._process_quotas(context, resources, project_id,
                                    project_quotas, quota_class,
                                    usages=project_usages,
                                    remains=remains)

    def _is_unlimited_value(self, v):
        """A helper method to check for unlimited value.
        """

        return v <= self.UNLIMITED_VALUE

    def _sum_quota_values(self, v1, v2):
        """A helper method that handles unlimited values when performing
        sum operation.
        """

        if self._is_unlimited_value(v1) or self._is_unlimited_value(v2):
            return self.UNLIMITED_VALUE
        return v1 + v2

    def _sub_quota_values(self, v1, v2):
        """A helper method that handles unlimited values when performing
        subtraction operation.
        """

        if self._is_unlimited_value(v1) or self._is_unlimited_value(v2):
            return self.UNLIMITED_VALUE
        return v1 - v2

    def get_settable_quotas(self, context, resources, project_id,
                            user_id=None):
        """Given a list of resources, retrieve the range of settable quotas for
        the given user or project.

        :param context: The request context, for access checks.
        :param resources: A dictionary of the registered resources.
        :param project_id: The ID of the project to return quotas for.
        :param user_id: The ID of the user to return quotas for.
        """

        settable_quotas = {}
        db_proj_quotas = objects.Quotas.get_all_by_project(context, project_id)
        project_quotas = self.get_project_quotas(context, resources,
                                                 project_id, remains=True,
                                                 project_quotas=db_proj_quotas)
        if user_id:
            setted_quotas = objects.Quotas.get_all_by_project_and_user(
                context, project_id, user_id)
            user_quotas = self.get_user_quotas(context, resources,
                                               project_id, user_id,
                                               project_quotas=db_proj_quotas,
                                               user_quotas=setted_quotas)
            for key, value in user_quotas.items():
                # Maximum is the remaining quota for a project (class/default
                # minus the sum of all user quotas in the project), plus the
                # given user's quota. So if the class/default is 20 and there
                # are two users each with quota of 5, then there is quota of
                # 10 remaining. The given user currently has quota of 5, so
                # the maximum you could update their quota to would be 15.
                # Class/default 20 - currently used in project 10 + current
                # user 5 = 15.
                maximum = \
                    self._sum_quota_values(project_quotas[key]['remains'],
                                           setted_quotas.get(key, 0))
                # This function is called for the quota_sets api and the
                # corresponding nova-manage command. The idea is when someone
                # attempts to update a quota, the value chosen must be at least
                # as much as the current usage and less than or equal to the
                # project limit less the sum of existing per user limits.
                minimum = value['in_use']
                settable_quotas[key] = {'minimum': minimum, 'maximum': maximum}
        else:
            for key, value in project_quotas.items():
                minimum = \
                    max(int(self._sub_quota_values(value['limit'],
                                                   value['remains'])),
                        int(value['in_use']))
                settable_quotas[key] = {'minimum': minimum, 'maximum': -1}
        return settable_quotas

    def _get_quotas(self, context, resources, keys, project_id=None,
                    user_id=None, project_quotas=None):
        """A helper method which retrieves the quotas for the specific
        resources identified by keys, and which apply to the current
        context.

        :param context: The request context, for access checks.
        :param resources: A dictionary of the registered resources.
        :param keys: A list of the desired quotas to retrieve.
        :param project_id: Specify the project_id if current context
                           is admin and admin wants to impact on
                           common user's tenant.
        :param user_id: Specify the user_id if current context
                        is admin and admin wants to impact on
                        common user.
        :param project_quotas: Quotas dictionary for the specified project.
        """

        # Filter resources
        desired = set(keys)
        sub_resources = {k: v for k, v in resources.items() if k in desired}

        # Make sure we accounted for all of them...
        if len(keys) != len(sub_resources):
            unknown = desired - set(sub_resources.keys())
            raise exception.QuotaResourceUnknown(unknown=sorted(unknown))

        if user_id:
            LOG.debug('Getting quotas for user %(user_id)s and project '
                      '%(project_id)s. Resources: %(keys)s',
                      {'user_id': user_id, 'project_id': project_id,
                       'keys': keys})
            # Grab and return the quotas (without usages)
            quotas = self.get_user_quotas(context, sub_resources,
                                          project_id, user_id,
                                          context.quota_class, usages=False,
                                          project_quotas=project_quotas)
        else:
            LOG.debug('Getting quotas for project %(project_id)s. Resources: '
                      '%(keys)s', {'project_id': project_id, 'keys': keys})
            # Grab and return the quotas (without usages)
            quotas = self.get_project_quotas(context, sub_resources,
                                             project_id,
                                             context.quota_class,
                                             usages=False,
                                             project_quotas=project_quotas)

        return {k: v['limit'] for k, v in quotas.items()}

    def limit_check(self, context, resources, values, project_id=None,
                    user_id=None):
        """Check simple quota limits.

        For limits--those quotas for which there is no usage
        synchronization function--this method checks that a set of
        proposed values are permitted by the limit restriction.

        This method will raise a QuotaResourceUnknown exception if a
        given resource is unknown or if it is not a simple limit
        resource.

        If any of the proposed values is over the defined quota, an
        OverQuota exception will be raised with the sorted list of the
        resources which are too high.  Otherwise, the method returns
        nothing.

        :param context: The request context, for access checks.
        :param resources: A dictionary of the registered resources.
        :param values: A dictionary of the values to check against the
                       quota.
        :param project_id: Specify the project_id if current context
                           is admin and admin wants to impact on
                           common user's tenant.
        :param user_id: Specify the user_id if current context
                        is admin and admin wants to impact on
                        common user.
        """
        _valid_method_call_check_resources(values, 'check', resources)

        # Ensure no value is less than zero
        unders = [key for key, val in values.items() if val < 0]
        if unders:
            raise exception.InvalidQuotaValue(unders=sorted(unders))

        # If project_id is None, then we use the project_id in context
        if project_id is None:
            project_id = context.project_id
        # If user id is None, then we use the user_id in context
        if user_id is None:
            user_id = context.user_id

        # Get the applicable quotas
        project_quotas = objects.Quotas.get_all_by_project(context, project_id)
        quotas = self._get_quotas(context, resources, values.keys(),
                                  project_id=project_id,
                                  project_quotas=project_quotas)
        user_quotas = self._get_quotas(context, resources, values.keys(),
                                       project_id=project_id,
                                       user_id=user_id,
                                       project_quotas=project_quotas)

        # Check the quotas and construct a list of the resources that
        # would be put over limit by the desired values
        overs = [key for key, val in values.items()
                 if quotas[key] >= 0 and quotas[key] < val or
                 (user_quotas[key] >= 0 and user_quotas[key] < val)]
        if overs:
            headroom = {}
            for key in overs:
                headroom[key] = min(
                    val for val in (quotas.get(key), project_quotas.get(key))
                    if val is not None
                )
            raise exception.OverQuota(overs=sorted(overs), quotas=quotas,
                                      usages={}, headroom=headroom)

    def limit_check_project_and_user(self, context, resources,
                                     project_values=None, user_values=None,
                                     project_id=None, user_id=None):
        """Check values (usage + desired delta) against quota limits.

        For limits--this method checks that a set of
        proposed values are permitted by the limit restriction.

        This method will raise a QuotaResourceUnknown exception if a
        given resource is unknown or if it is not a simple limit
        resource.

        If any of the proposed values is over the defined quota, an
        OverQuota exception will be raised with the sorted list of the
        resources which are too high.  Otherwise, the method returns
        nothing.

        :param context: The request context, for access checks
        :param resources: A dictionary of the registered resources
        :param project_values: Optional dict containing the resource values to
                            check against project quota,
                            e.g. {'instances': 1, 'cores': 2, 'memory_mb': 512}
        :param user_values: Optional dict containing the resource values to
                            check against user quota,
                            e.g. {'instances': 1, 'cores': 2, 'memory_mb': 512}
        :param project_id: Optional project_id for scoping the limit check to a
                           different project than in the context
        :param user_id: Optional user_id for scoping the limit check to a
                        different user than in the context
        """
        if project_values is None:
            project_values = {}
        if user_values is None:
            user_values = {}

        _valid_method_call_check_resources(project_values, 'check', resources)
        _valid_method_call_check_resources(user_values, 'check', resources)

        if not any([project_values, user_values]):
            raise exception.Invalid(
                'Must specify at least one of project_values or user_values '
                'for the limit check.')

        # Ensure no value is less than zero
        for vals in (project_values, user_values):
            unders = [key for key, val in vals.items() if val < 0]
            if unders:
                raise exception.InvalidQuotaValue(unders=sorted(unders))

        # Get a set of all keys for calling _get_quotas() so we get all of the
        # resource limits we need.
        all_keys = set(project_values).union(user_values)

        # Keys that are in both project_values and user_values need to be
        # checked against project quota and user quota, respectively.
        # Keys that are not in both only need to be checked against project
        # quota or user quota, if it is defined. Separate the keys that don't
        # need to be checked against both quotas, merge them into one dict,
        # and remove them from project_values and user_values.
        keys_to_merge = set(project_values).symmetric_difference(user_values)
        merged_values = {}
        for key in keys_to_merge:
            # The key will be either in project_values or user_values based on
            # the earlier symmetric_difference. Default to 0 in case the found
            # value is 0 and won't take precedence over a None default.
            merged_values[key] = (project_values.get(key, 0) or
                                  user_values.get(key, 0))
            project_values.pop(key, None)
            user_values.pop(key, None)

        # If project_id is None, then we use the project_id in context
        if project_id is None:
            project_id = context.project_id
        # If user id is None, then we use the user_id in context
        if user_id is None:
            user_id = context.user_id

        # Get the applicable quotas. They will be merged together (taking the
        # min limit) if project_values and user_values were not specified
        # together.

        # per project quota limits (quotas that have no concept of
        # user-scoping: <none>)
        project_quotas = objects.Quotas.get_all_by_project(context, project_id)
        # per user quotas, project quota limits (for quotas that have
        # user-scoping, limits for the project)
        quotas = self._get_quotas(context, resources, all_keys,
                                  project_id=project_id,
                                  project_quotas=project_quotas)
        # per user quotas, user quota limits (for quotas that have
        # user-scoping, the limits for the user)
        user_quotas = self._get_quotas(context, resources, all_keys,
                                       project_id=project_id,
                                       user_id=user_id,
                                       project_quotas=project_quotas)

        if merged_values:
            # This is for resources that are not counted across a project and
            # must pass both the quota for the project and the quota for the
            # user.
            # Combine per user project quotas and user_quotas for use in the
            # checks, taking the minimum limit between the two.
            merged_quotas = copy.deepcopy(quotas)
            for k, v in user_quotas.items():
                if k in merged_quotas:
                    merged_quotas[k] = min(merged_quotas[k], v)
                else:
                    merged_quotas[k] = v

            # Check the quotas and construct a list of the resources that
            # would be put over limit by the desired values
            overs = [key for key, val in merged_values.items()
                     if merged_quotas[key] >= 0 and merged_quotas[key] < val]
            if overs:
                headroom = {}
                for key in overs:
                    headroom[key] = merged_quotas[key]
                raise exception.OverQuota(overs=sorted(overs),
                                          quotas=merged_quotas, usages={},
                                          headroom=headroom)

        # This is for resources that are counted across a project and
        # across a user (instances, cores, ram, server_groups). The
        # project_values must pass the quota for the project and the
        # user_values must pass the quota for the user.
        over_user_quota = False
        overs = []
        for key in user_values.keys():
            # project_values and user_values should contain the same keys or
            # be empty after the keys in the symmetric_difference were removed
            # from both dicts.
            if quotas[key] >= 0 and quotas[key] < project_values[key]:
                overs.append(key)
            elif (user_quotas[key] >= 0 and
                  user_quotas[key] < user_values[key]):
                overs.append(key)
                over_user_quota = True
        if overs:
            quotas_exceeded = user_quotas if over_user_quota else quotas
            headroom = {}
            for key in overs:
                headroom[key] = quotas_exceeded[key]
            raise exception.OverQuota(overs=sorted(overs),
                                      quotas=quotas_exceeded, usages={},
                                      headroom=headroom)


class NoopQuotaDriver(object):
    """Driver that turns quotas calls into no-ops and pretends that quotas
    for all resources are unlimited. This can be used if you do not
    wish to have any quota checking.
    """

    def get_reserved(self):
        # Noop has always returned -1 for reserved
        return -1

    def get_defaults(self, context, resources):
        """Given a list of resources, retrieve the default quotas.

        :param context: The request context, for access checks.
        :param resources: A dictionary of the registered resources.
        """
        quotas = {}
        for resource in resources.values():
            quotas[resource.name] = -1
        return quotas

    def get_class_quotas(self, context, resources, quota_class):
        """Given a list of resources, retrieve the quotas for the given
        quota class.

        :param context: The request context, for access checks.
        :param resources: A dictionary of the registered resources.
        :param quota_class: The name of the quota class to return
                            quotas for.
        """
        quotas = {}
        for resource in resources.values():
            quotas[resource.name] = -1
        return quotas

    def _get_noop_quotas(self, resources, usages=None, remains=False):
        quotas = {}
        for resource in resources.values():
            quotas[resource.name] = {}
            quotas[resource.name]['limit'] = -1
            if usages:
                quotas[resource.name]['in_use'] = -1
            if remains:
                quotas[resource.name]['remains'] = -1
        return quotas

    def get_user_quotas(self, context, resources, project_id, user_id,
                        quota_class=None,
                        usages=True):
        """Given a list of resources, retrieve the quotas for the given
        user and project.

        :param context: The request context, for access checks.
        :param resources: A dictionary of the registered resources.
        :param project_id: The ID of the project to return quotas for.
        :param user_id: The ID of the user to return quotas for.
        :param quota_class: If project_id != context.project_id, the
                            quota class cannot be determined.  This
                            parameter allows it to be specified.  It
                            will be ignored if project_id ==
                            context.project_id.
        :param usages: If True, the current counts will also be returned.
        """
        return self._get_noop_quotas(resources, usages=usages)

    def get_project_quotas(self, context, resources, project_id,
                           quota_class=None,
                           usages=True, remains=False):
        """Given a list of resources, retrieve the quotas for the given
        project.

        :param context: The request context, for access checks.
        :param resources: A dictionary of the registered resources.
        :param project_id: The ID of the project to return quotas for.
        :param quota_class: If project_id != context.project_id, the
                            quota class cannot be determined.  This
                            parameter allows it to be specified.  It
                            will be ignored if project_id ==
                            context.project_id.
        :param usages: If True, the current counts will also be returned.
        :param remains: If True, the current remains of the project will
                        will be returned.
        """
        return self._get_noop_quotas(resources, usages=usages, remains=remains)

    def get_settable_quotas(self, context, resources, project_id,
                            user_id=None):
        """Given a list of resources, retrieve the range of settable quotas for
        the given user or project.

        :param context: The request context, for access checks.
        :param resources: A dictionary of the registered resources.
        :param project_id: The ID of the project to return quotas for.
        :param user_id: The ID of the user to return quotas for.
        """
        quotas = {}
        for resource in resources.values():
            quotas[resource.name] = {'minimum': 0, 'maximum': -1}
        return quotas

    def limit_check(self, context, resources, values, project_id=None,
                    user_id=None):
        """Check simple quota limits.

        For limits--those quotas for which there is no usage
        synchronization function--this method checks that a set of
        proposed values are permitted by the limit restriction.

        This method will raise a QuotaResourceUnknown exception if a
        given resource is unknown or if it is not a simple limit
        resource.

        If any of the proposed values is over the defined quota, an
        OverQuota exception will be raised with the sorted list of the
        resources which are too high.  Otherwise, the method returns
        nothing.

        :param context: The request context, for access checks.
        :param resources: A dictionary of the registered resources.
        :param values: A dictionary of the values to check against the
                       quota.
        :param project_id: Specify the project_id if current context
                           is admin and admin wants to impact on
                           common user's tenant.
        :param user_id: Specify the user_id if current context
                        is admin and admin wants to impact on
                        common user.
        """
        pass

    def limit_check_project_and_user(self, context, resources,
                                     project_values=None, user_values=None,
                                     project_id=None, user_id=None):
        """Check values against quota limits.

        For limits--this method checks that a set of
        proposed values are permitted by the limit restriction.

        This method will raise a QuotaResourceUnknown exception if a
        given resource is unknown or if it is not a simple limit
        resource.

        If any of the proposed values is over the defined quota, an
        OverQuota exception will be raised with the sorted list of the
        resources which are too high.  Otherwise, the method returns
        nothing.

        :param context: The request context, for access checks
        :param resources: A dictionary of the registered resources
        :param project_values: Optional dict containing the resource values to
                            check against project quota,
                            e.g. {'instances': 1, 'cores': 2, 'memory_mb': 512}
        :param user_values: Optional dict containing the resource values to
                            check against user quota,
                            e.g. {'instances': 1, 'cores': 2, 'memory_mb': 512}
        :param project_id: Optional project_id for scoping the limit check to a
                           different project than in the context
        :param user_id: Optional user_id for scoping the limit check to a
                        different user than in the context
        """
        pass


class UnifiedLimitsDriver(NoopQuotaDriver):
    """Ease migration to new unified limits code.

    Help ease migration to unified limits by ensuring the old code
    paths still work with unified limits. Eventually the expectation is
    all this legacy quota code will go away, leaving the new simpler code
    """

    def __init__(self):
        LOG.warning("The Unified Limits Quota Driver is experimental and "
                    "is under active development. Do not use this driver.")

    def get_reserved(self):
        # To make unified limits APIs the same as the DB driver, return 0
        return 0

    def get_class_quotas(self, context, resources, quota_class):
        """Given a list of resources, retrieve the quotas for the given
        quota class.

        :param context: The request context, for access checks.
        :param resources: A dictionary of the registered resources.
        :param quota_class: Placeholder, we always assume default quota class.
        """
        # NOTE(johngarbutt): ignoring quota_class, as ignored in noop driver
        return self.get_defaults(context, resources)

    def get_defaults(self, context, resources):
        local_limits = local_limit.get_legacy_default_limits()
        # Note we get 0 if there is no registered limit,
        # to mirror oslo_limit behaviour when there is no registered limit
        placement_limits = placement_limit.get_legacy_default_limits()
        quotas = {}
        for resource in resources.values():
            if resource.name in placement_limits:
                quotas[resource.name] = placement_limits[resource.name]
            else:
                # return -1 for things like security_group_rules
                # that are neither a keystone limit or a local limit
                quotas[resource.name] = local_limits.get(resource.name, -1)

        return quotas

    def get_project_quotas(self, context, resources, project_id,
                           quota_class=None,
                           usages=True, remains=False):
        if quota_class is not None:
            raise NotImplementedError("quota_class")

        if remains:
            raise NotImplementedError("remains")

        local_limits = local_limit.get_legacy_default_limits()
        # keystone limits always returns core, ram and instances
        # if nothing set in keystone, we get back 0, i.e. don't allow
        placement_limits = placement_limit.get_legacy_project_limits(
            project_id)

        project_quotas = {}
        for resource in resources.values():
            if resource.name in placement_limits:
                limit = placement_limits[resource.name]
            else:
                # return -1 for things like security_group_rules
                # that are neither a keystone limit or a local limit
                limit = local_limits.get(resource.name, -1)
            project_quotas[resource.name] = {"limit": limit}

        if usages:
            local_in_use = local_limit.get_in_use(context, project_id)
            p_in_use = placement_limit.get_legacy_counts(context, project_id)

            for resource in resources.values():
                # default to 0 for resources that are deprecated,
                # i.e. not in keystone or local limits, such that we
                # are API compatible with what was returned with
                # the db driver, even though noop driver returned -1
                usage_count = 0
                if resource.name in local_in_use:
                    usage_count = local_in_use[resource.name]
                if resource.name in p_in_use:
                    usage_count = p_in_use[resource.name]
                project_quotas[resource.name]["in_use"] = usage_count

        return project_quotas

    def get_user_quotas(self, context, resources, project_id, user_id,
                        quota_class=None, usages=True):
        return self.get_project_quotas(context, resources, project_id,
                                       quota_class, usages)


class BaseResource(object):
    """Describe a single resource for quota checking."""

    def __init__(self, name, flag=None):
        """Initializes a Resource.

        :param name: The name of the resource, i.e., "instances".
        :param flag: The name of the flag or configuration option
                     which specifies the default value of the quota
                     for this resource.
        """

        self.name = name
        self.flag = flag

    @property
    def default(self):
        """Return the default value of the quota."""
        return CONF.quota[self.flag] if self.flag else -1


class AbsoluteResource(BaseResource):
    """Describe a resource that does not correspond to database objects."""
    valid_method = 'check'


class CountableResource(AbsoluteResource):
    """Describe a resource where the counts aren't based solely on the
    project ID.
    """

    def __init__(self, name, count_as_dict, flag=None):
        """Initializes a CountableResource.

        Countable resources are those resources which directly
        correspond to objects in the database, but for which a count
        by project ID is inappropriate e.g. keypairs
        A CountableResource must be constructed with a counting
        function, which will be called to determine the current counts
        of the resource.

        The counting function will be passed the context, along with
        the extra positional and keyword arguments that are passed to
        Quota.count_as_dict().  It should return a dict specifying the
        count scoped to a project and/or a user.

        Example count of instances, cores, or ram returned as a rollup
        of all the resources since we only want to query the instances
        table once, not multiple times, for each resource.
        Instances, cores, and ram are counted across a project and
        across a user:

            {'project': {'instances': 5, 'cores': 8, 'ram': 4096},
             'user': {'instances': 1, 'cores': 2, 'ram': 512}}

        Example count of server groups keeping a consistent format.
        Server groups are counted across a project and across a user:

            {'project': {'server_groups': 7},
             'user': {'server_groups': 2}}

        Example count of key pairs keeping a consistent format.
        Key pairs are counted across a user only:

            {'user': {'key_pairs': 5}}

        Note that this counting is not performed in a transaction-safe
        manner.  This resource class is a temporary measure to provide
        required functionality, until a better approach to solving
        this problem can be evolved.

        :param name: The name of the resource, i.e., "instances".
        :param count_as_dict: A callable which returns the count of the
                              resource as a dict.  The arguments passed are as
                              described above.
        :param flag: The name of the flag or configuration option
                     which specifies the default value of the quota
                     for this resource.
        """

        super(CountableResource, self).__init__(name, flag=flag)
        self.count_as_dict = count_as_dict


class QuotaEngine(object):
    """Represent the set of recognized quotas."""

    def __init__(self, quota_driver=None, resources=None):
        """Initialize a Quota object.

        :param quota_driver: a QuotaDriver object (only used in testing. if
                             None (default), instantiates a driver from the
                             CONF.quota.driver option)
        :param resources: iterable of Resource objects
        """
        resources = resources or []
        self._resources = {
            resource.name: resource for resource in resources
        }
        # NOTE(mriedem): quota_driver is ever only supplied in tests with a
        # fake driver.
        self.__driver_override = quota_driver
        self.__driver = None
        self.__driver_name = None

    @property
    def _driver(self):
        if self.__driver_override:
            return self.__driver_override

        # NOTE(johngarbutt) to allow unit tests to change the driver by
        # simply overriding config, double check if we have the correct
        # driver cached before we return the currently cached driver
        driver_name_in_config = CONF.quota.driver
        if self.__driver_name != driver_name_in_config:
            self.__driver = importutils.import_object(driver_name_in_config)
            self.__driver_name = driver_name_in_config

        return self.__driver

    def get_defaults(self, context):
        """Retrieve the default quotas.

        :param context: The request context, for access checks.
        """

        return self._driver.get_defaults(context, self._resources)

    def get_class_quotas(self, context, quota_class):
        """Retrieve the quotas for the given quota class.

        :param context: The request context, for access checks.
        :param quota_class: The name of the quota class to return
                            quotas for.
        """

        return self._driver.get_class_quotas(context, self._resources,
                                             quota_class)

    def get_user_quotas(self, context, project_id, user_id, quota_class=None,
                        usages=True):
        """Retrieve the quotas for the given user and project.

        :param context: The request context, for access checks.
        :param project_id: The ID of the project to return quotas for.
        :param user_id: The ID of the user to return quotas for.
        :param quota_class: If project_id != context.project_id, the
                            quota class cannot be determined.  This
                            parameter allows it to be specified.
        :param usages: If True, the current counts will also be returned.
        """

        return self._driver.get_user_quotas(context, self._resources,
                                            project_id, user_id,
                                            quota_class=quota_class,
                                            usages=usages)

    def get_project_quotas(self, context, project_id, quota_class=None,
                           usages=True, remains=False):
        """Retrieve the quotas for the given project.

        :param context: The request context, for access checks.
        :param project_id: The ID of the project to return quotas for.
        :param quota_class: If project_id != context.project_id, the
                            quota class cannot be determined.  This
                            parameter allows it to be specified.
        :param usages: If True, the current counts will also be returned.
        :param remains: If True, the current remains of the project will
                        will be returned.
        """

        return self._driver.get_project_quotas(context, self._resources,
                                              project_id,
                                              quota_class=quota_class,
                                              usages=usages,
                                              remains=remains)

    def get_settable_quotas(self, context, project_id, user_id=None):
        """Given a list of resources, retrieve the range of settable quotas for
        the given user or project.

        :param context: The request context, for access checks.
        :param project_id: The ID of the project to return quotas for.
        :param user_id: The ID of the user to return quotas for.
        """

        return self._driver.get_settable_quotas(context, self._resources,
                                                project_id,
                                                user_id=user_id)

    def count_as_dict(self, context, resource, *args, **kwargs):
        """Count a resource and return a dict.

        For countable resources, invokes the count_as_dict() function and
        returns its result.  Arguments following the context and
        resource are passed directly to the count function declared by
        the resource.

        :param context: The request context, for access checks.
        :param resource: The name of the resource, as a string.
        :returns: A dict containing the count(s) for the resource, for example:
                    {'project': {'instances': 2, 'cores': 4, 'ram': 1024},
                     'user': {'instances': 1, 'cores': 2, 'ram': 512}}

                  another example:
                    {'user': {'key_pairs': 5}}
        """

        # Get the resource
        res = self._resources.get(resource)
        if not res or not hasattr(res, 'count_as_dict'):
            raise exception.QuotaResourceUnknown(unknown=[resource])

        return res.count_as_dict(context, *args, **kwargs)

    # TODO(melwitt): This can be removed once no old code can call
    # limit_check(). It will be replaced with limit_check_project_and_user().
    def limit_check(self, context, project_id=None, user_id=None, **values):
        """Check simple quota limits.

        For limits--those quotas for which there is no usage
        synchronization function--this method checks that a set of
        proposed values are permitted by the limit restriction.  The
        values to check are given as keyword arguments, where the key
        identifies the specific quota limit to check, and the value is
        the proposed value.

        This method will raise a QuotaResourceUnknown exception if a
        given resource is unknown or if it is not a simple limit
        resource.

        If any of the proposed values is over the defined quota, an
        OverQuota exception will be raised with the sorted list of the
        resources which are too high.  Otherwise, the method returns
        nothing.

        :param context: The request context, for access checks.
        :param project_id: Specify the project_id if current context
                           is admin and admin wants to impact on
                           common user's tenant.
        :param user_id: Specify the user_id if current context
                        is admin and admin wants to impact on
                        common user.
        """

        return self._driver.limit_check(context, self._resources, values,
                                        project_id=project_id, user_id=user_id)

    def limit_check_project_and_user(self, context, project_values=None,
                                     user_values=None, project_id=None,
                                     user_id=None):
        """Check values against quota limits.

        For limits--this method checks that a set of
        proposed values are permitted by the limit restriction.

        This method will raise a QuotaResourceUnknown exception if a
        given resource is unknown or if it is not a simple limit
        resource.

        If any of the proposed values is over the defined quota, an
        OverQuota exception will be raised with the sorted list of the
        resources which are too high.  Otherwise, the method returns
        nothing.

        :param context: The request context, for access checks
        :param project_values: Optional dict containing the resource values to
                            check against project quota,
                            e.g. {'instances': 1, 'cores': 2, 'memory_mb': 512}
        :param user_values: Optional dict containing the resource values to
                            check against user quota,
                            e.g. {'instances': 1, 'cores': 2, 'memory_mb': 512}
        :param project_id: Optional project_id for scoping the limit check to a
                           different project than in the context
        :param user_id: Optional user_id for scoping the limit check to a
                        different user than in the context
        """
        return self._driver.limit_check_project_and_user(
            context, self._resources, project_values=project_values,
            user_values=user_values, project_id=project_id, user_id=user_id)

    @property
    def resources(self):
        return sorted(self._resources.keys())

    def get_reserved(self):
        return self._driver.get_reserved()


@api_db_api.context_manager.reader
def _user_id_queued_for_delete_populated(context, project_id=None):
    """Determine whether user_id and queued_for_delete are set.

    This will be used to determine whether we need to fall back on
    the legacy quota counting method (if we cannot rely on counting
    instance mappings for the instance count). If any records with user_id=None
    and queued_for_delete=False are found, we need to fall back to the legacy
    counting method. If any records with queued_for_delete=None are found, we
    need to fall back to the legacy counting method.

    Note that this check specifies queued_for_deleted=False, which excludes
    deleted and SOFT_DELETED instances. The 'populate_user_id' data migration
    migrates SOFT_DELETED instances because they could be restored at any time
    in the future. However, for this quota-check-time method, it is acceptable
    to ignore SOFT_DELETED instances, since we just want to know if it is safe
    to use instance mappings to count instances at this point in time (and
    SOFT_DELETED instances do not count against quota limits).

    We also want to fall back to the legacy counting method if we detect any
    records that have not yet populated the queued_for_delete field. We do this
    instead of counting queued_for_delete=None records since that might not
    accurately reflect the project or project user's quota usage.

    :param project_id: The project to check
    :returns: True if user_id is set for all non-deleted instances and
              queued_for_delete is set for all instances, else False
    """
    user_id_not_populated = sql.and_(
        api_models.InstanceMapping.user_id == sql.null(),
        api_models.InstanceMapping.queued_for_delete == sql.false())
    # If either queued_for_delete or user_id are unmigrated, we will return
    # False.
    unmigrated_filter = sql.or_(
        api_models.InstanceMapping.queued_for_delete == sql.null(),
        user_id_not_populated)
    query = context.session.query(api_models.InstanceMapping).filter(
        unmigrated_filter)
    if project_id:
        query = query.filter_by(project_id=project_id)
    return not context.session.query(query.exists()).scalar()


def _keypair_get_count_by_user(context, user_id):
    count = objects.KeyPairList.get_count_by_user(context, user_id)
    return {'user': {'key_pairs': count}}


def _server_group_count_members_by_user_legacy(context, group, user_id):
    filters = {'deleted': False, 'user_id': user_id, 'uuid': group.members}

    def group_member_uuids(cctxt):
        return {inst.uuid for inst in objects.InstanceList.get_by_filters(
            cctxt, filters, expected_attrs=[])}

    # Ignore any duplicates since build requests and instances can co-exist
    # for a short window of time after the instance is created in a cell but
    # before the build request is deleted.
    instance_uuids = set()

    # NOTE(melwitt): Counting across cells for instances means we will miss
    # counting resources if a cell is down.
    per_cell = nova_context.scatter_gather_all_cells(
        context, group_member_uuids)
    for uuids in per_cell.values():
        instance_uuids |= uuids

    # Count build requests using the same filters to catch group members
    # that are not yet created in a cell.
    build_requests = objects.BuildRequestList.get_by_filters(context, filters)
    for build_request in build_requests:
        instance_uuids.add(build_request.instance_uuid)

    return {'user': {'server_group_members': len(instance_uuids)}}


def is_qfd_populated(context):
    """Check if user_id and queued_for_delete fields are populated.

    This method is related to counting quota usage from placement. It is not
    yet possible to count instances from placement, so in the meantime we can
    use instance mappings for counting. This method is used to determine
    whether the user_id and queued_for_delete columns are populated in the API
    database's instance_mappings table. Instance mapping records are not
    deleted from the database until the database is archived, so
    queued_for_delete tells us whether or not we should count them for instance
    quota usage. The user_id field enables us to scope instance quota usage to
    a user (legacy quota).

    Scoping instance quota to a user is only possible
    when counting quota usage from placement is configured and unified limits
    is not configured. When unified limits is configured, quotas are scoped
    only to projects.

    In the future when it is possible to count instance usage from placement,
    this method will no longer be needed.
    """
    global UID_QFD_POPULATED_CACHE_ALL
    if not UID_QFD_POPULATED_CACHE_ALL:
        LOG.debug('Checking whether user_id and queued_for_delete are '
                  'populated for all projects')
        UID_QFD_POPULATED_CACHE_ALL = _user_id_queued_for_delete_populated(
            context)

    return UID_QFD_POPULATED_CACHE_ALL


def _server_group_count_members_by_user(context, group, user_id):
    """Get the count of server group members for a group by user.

    :param context: The request context for database access
    :param group: The InstanceGroup object with members to count
    :param user_id: The user_id to count across
    :returns: A dict containing the user-scoped count. For example:

                {'user': 'server_group_members': <count across user>}}
    """
    # Because server group members quota counting is not scoped to a project,
    # but scoped to a particular InstanceGroup and user, we have no reasonable
    # way of pruning down our migration check to only a subset of all instance
    # mapping records.
    # So, we check whether user_id/queued_for_delete is populated for all
    # records and cache the result to prevent unnecessary checking once the
    # data migration has been completed.
    if is_qfd_populated(context):
        count = objects.InstanceMappingList.get_count_by_uuids_and_user(
            context, group.members, user_id)
        return {'user': {'server_group_members': count}}

    LOG.warning('Falling back to legacy quota counting method for server '
                'group members')
    return _server_group_count_members_by_user_legacy(context, group,
                                                      user_id)


def _instances_cores_ram_count_legacy(context, project_id, user_id=None):
    """Get the counts of instances, cores, and ram in cell databases.

    :param context: The request context for database access
    :param project_id: The project_id to count across
    :param user_id: The user_id to count across
    :returns: A dict containing the project-scoped counts and user-scoped
              counts if user_id is specified. For example:

                {'project': {'instances': <count across project>,
                             'cores': <count across project>,
                             'ram': <count across project>},
                 'user': {'instances': <count across user>,
                          'cores': <count across user>,
                          'ram': <count across user>}}
    """
    # NOTE(melwitt): Counting across cells for instances, cores, and ram means
    # we will miss counting resources if a cell is down.
    # NOTE(tssurya): We only go into those cells in which the tenant has
    # instances. We could optimize this to avoid the CellMappingList query
    # for single-cell deployments by checking the cell cache and only doing
    # this filtering if there is more than one non-cell0 cell.
    # TODO(tssurya): Consider adding a scatter_gather_cells_for_project
    # variant that makes this native to nova.context.
    if CONF.api.instance_list_per_project_cells:
        cell_mappings = objects.CellMappingList.get_by_project_id(
            context, project_id)
    else:
        nova_context.load_cells()
        cell_mappings = nova_context.CELLS
    results = nova_context.scatter_gather_cells(
        context, cell_mappings, nova_context.CELL_TIMEOUT,
        objects.InstanceList.get_counts, project_id, user_id=user_id)
    total_counts = {'project': {'instances': 0, 'cores': 0, 'ram': 0}}
    if user_id:
        total_counts['user'] = {'instances': 0, 'cores': 0, 'ram': 0}
    for result in results.values():
        if not nova_context.is_cell_failure_sentinel(result):
            for resource, count in result['project'].items():
                total_counts['project'][resource] += count
            if user_id:
                for resource, count in result['user'].items():
                    total_counts['user'][resource] += count
    return total_counts


def _cores_ram_count_placement(context, project_id, user_id=None):
    return report.report_client_singleton().get_usages_counts_for_quota(
        context, project_id, user_id=user_id)


def _instances_cores_ram_count_api_db_placement(context, project_id,
                                                user_id=None):
    # Will return a dict with format: {'project': {'instances': M},
    #                                  'user': {'instances': N}}
    # where the 'user' key is optional.
    total_counts = objects.InstanceMappingList.get_counts(context,
                                                          project_id,
                                                          user_id=user_id)
    cores_ram_counts = _cores_ram_count_placement(context, project_id,
                                                  user_id=user_id)
    total_counts['project'].update(cores_ram_counts['project'])
    if 'user' in total_counts:
        total_counts['user'].update(cores_ram_counts['user'])
    return total_counts


def _instances_cores_ram_count(context, project_id, user_id=None):
    """Get the counts of instances, cores, and ram.

    :param context: The request context for database access
    :param project_id: The project_id to count across
    :param user_id: The user_id to count across
    :returns: A dict containing the project-scoped counts and user-scoped
              counts if user_id is specified. For example:

                {'project': {'instances': <count across project>,
                             'cores': <count across project>,
                             'ram': <count across project>},
                 'user': {'instances': <count across user>,
                          'cores': <count across user>,
                          'ram': <count across user>}}
    """
    global UID_QFD_POPULATED_CACHE_BY_PROJECT
    if CONF.quota.count_usage_from_placement:
        # If a project has all user_id and queued_for_delete data populated,
        # cache the result to avoid needless database checking in the future.
        if (not UID_QFD_POPULATED_CACHE_ALL and
                project_id not in UID_QFD_POPULATED_CACHE_BY_PROJECT):
            LOG.debug('Checking whether user_id and queued_for_delete are '
                      'populated for project_id %s', project_id)
            uid_qfd_populated = _user_id_queued_for_delete_populated(
                context, project_id)
            if uid_qfd_populated:
                UID_QFD_POPULATED_CACHE_BY_PROJECT.add(project_id)
        else:
            uid_qfd_populated = True
        if uid_qfd_populated:
            return _instances_cores_ram_count_api_db_placement(context,
                                                               project_id,
                                                               user_id=user_id)
        LOG.warning('Falling back to legacy quota counting method for '
                    'instances, cores, and ram')
    return _instances_cores_ram_count_legacy(context, project_id,
                                             user_id=user_id)


def _server_group_count(context, project_id, user_id=None):
    """Get the counts of server groups in the database.

    :param context: The request context for database access
    :param project_id: The project_id to count across
    :param user_id: The user_id to count across
    :returns: A dict containing the project-scoped counts and user-scoped
              counts if user_id is specified. For example:

                {'project': {'server_groups': <count across project>},
                 'user': {'server_groups': <count across user>}}
    """
    return objects.InstanceGroupList.get_counts(context, project_id,
                                                user_id=user_id)


QUOTAS = QuotaEngine(
    resources=[
        CountableResource(
            'instances', _instances_cores_ram_count, 'instances'),
        CountableResource(
            'cores', _instances_cores_ram_count, 'cores'),
        CountableResource(
            'ram', _instances_cores_ram_count, 'ram'),
        AbsoluteResource(
            'metadata_items', 'metadata_items'),
        AbsoluteResource(
            'injected_files', 'injected_files'),
        AbsoluteResource(
            'injected_file_content_bytes', 'injected_file_content_bytes'),
        AbsoluteResource(
            'injected_file_path_bytes', 'injected_file_path_length'),
        CountableResource(
            'key_pairs', _keypair_get_count_by_user, 'key_pairs'),
        CountableResource(
            'server_groups', _server_group_count, 'server_groups'),
        CountableResource(
            'server_group_members', _server_group_count_members_by_user,
            'server_group_members'),
        # Deprecated nova-network quotas, retained to avoid changing API
        # responses
        AbsoluteResource('fixed_ips'),
        AbsoluteResource('floating_ips'),
        AbsoluteResource('security_groups'),
        AbsoluteResource('security_group_rules'),
    ],
)


def _valid_method_call_check_resource(name, method, resources):
    if name not in resources:
        raise exception.InvalidQuotaMethodUsage(method=method, res=name)
    res = resources[name]

    if res.valid_method != method:
        raise exception.InvalidQuotaMethodUsage(method=method, res=name)


def _valid_method_call_check_resources(resource_values, method, resources):
    """A method to check whether the resource can use the quota method.

    :param resource_values: Dict containing the resource names and values
    :param method: The quota method to check
    :param resources: Dict containing Resource objects to validate against
    """

    for name in resource_values.keys():
        _valid_method_call_check_resource(name, method, resources)

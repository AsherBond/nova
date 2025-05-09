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

from oslo_serialization import jsonutils
from oslo_utils import versionutils

from nova.db.main import api as db
from nova.objects import base
from nova.objects import fields


@base.NovaObjectRegistry.register
class InstancePCIRequest(base.NovaObject):
    # Version 1.0: Initial version
    # Version 1.1: Added request_id field
    # Version 1.2: Added numa_policy field
    # Version 1.3: Added requester_id field
    # Version 1.4: Added 'socket' to numa_policy field
    VERSION = '1.4'

    # Possible sources for a PCI request:
    # FLAVOR_ALIAS : Request originated from a flavor alias.
    # NEUTRON_PORT : Request originated from a neutron port.
    FLAVOR_ALIAS = 0
    NEUTRON_PORT = 1

    fields = {
        'count': fields.IntegerField(),
        'spec': fields.ListOfDictOfNullableStringsField(),
        'alias_name': fields.StringField(nullable=True),
        # Note(moshele): is_new is deprecated and should be removed
        # on major version bump
        'is_new': fields.BooleanField(default=False),
        'request_id': fields.UUIDField(nullable=True),
        'requester_id': fields.StringField(nullable=True),
        'numa_policy': fields.PCINUMAAffinityPolicyField(nullable=True),
    }

    @property
    def source(self):
        # PCI requests originate from two sources: instance flavor alias and
        # neutron SR-IOV ports.
        # SR-IOV ports pci_request don't have an alias_name.
        return (InstancePCIRequest.NEUTRON_PORT if self.alias_name is None
                else InstancePCIRequest.FLAVOR_ALIAS)

    def obj_load_attr(self, attr):
        setattr(self, attr, None)

    def obj_make_compatible(self, primitive, target_version):
        super(InstancePCIRequest, self).obj_make_compatible(primitive,
                                                            target_version)
        target_version = versionutils.convert_version_to_tuple(target_version)
        if target_version < (1, 3) and 'requester_id' in primitive:
            del primitive['requester_id']
        if target_version < (1, 2) and 'numa_policy' in primitive:
            del primitive['numa_policy']
        if target_version < (1, 1) and 'request_id' in primitive:
            del primitive['request_id']

    def is_live_migratable(self):
        return (
            "spec" in self and
            self.spec is not None and
            all(
                spec.get("live_migratable") == "true" for spec in self.spec
            )
        )


@base.NovaObjectRegistry.register
class InstancePCIRequests(base.NovaObject):
    # Version 1.0: Initial version
    # Version 1.1: InstancePCIRequest 1.1
    VERSION = '1.1'

    fields = {
        'instance_uuid': fields.UUIDField(),
        'requests': fields.ListOfObjectsField('InstancePCIRequest'),
    }

    @classmethod
    def obj_from_db(cls, context, instance_uuid, db_requests):
        self = cls(context=context, requests=[],
                   instance_uuid=instance_uuid)
        if db_requests is not None:
            requests = jsonutils.loads(db_requests)
        else:
            requests = []
        for request in requests:
            # Note(moshele): is_new is deprecated and therefore we load it
            # with default value of False
            request_obj = InstancePCIRequest(
                count=request['count'], spec=request['spec'],
                alias_name=request['alias_name'], is_new=False,
                numa_policy=request.get('numa_policy',
                                        fields.PCINUMAAffinityPolicy.LEGACY),
                request_id=request['request_id'],
                requester_id=request.get('requester_id'))
            request_obj.obj_reset_changes()
            self.requests.append(request_obj)
        self.obj_reset_changes()
        return self

    @base.remotable_classmethod
    def get_by_instance_uuid(cls, context, instance_uuid):
        db_pci_requests = db.instance_extra_get_by_instance_uuid(
                context, instance_uuid, columns=['pci_requests'])
        if db_pci_requests is not None:
            db_pci_requests = db_pci_requests['pci_requests']
        return cls.obj_from_db(context, instance_uuid, db_pci_requests)

    @staticmethod
    def _load_legacy_requests(sysmeta_value, is_new=False):
        if sysmeta_value is None:
            return []
        requests = []
        db_requests = jsonutils.loads(sysmeta_value)
        for db_request in db_requests:
            request = InstancePCIRequest(
                count=db_request['count'], spec=db_request['spec'],
                alias_name=db_request['alias_name'], is_new=is_new)
            request.obj_reset_changes()
            requests.append(request)
        return requests

    @classmethod
    def get_by_instance(cls, context, instance):
        # NOTE (baoli): not all callers are passing instance as object yet.
        # Therefore, use the dict syntax in this routine
        if 'pci_requests' in instance['system_metadata']:
            # NOTE(danms): This instance hasn't been converted to use
            # instance_extra yet, so extract the data from sysmeta
            sysmeta = instance['system_metadata']
            _requests = (
                cls._load_legacy_requests(sysmeta['pci_requests']) +
                cls._load_legacy_requests(sysmeta.get('new_pci_requests'),
                                          is_new=True))
            requests = cls(instance_uuid=instance['uuid'], requests=_requests)
            requests.obj_reset_changes()
            return requests
        else:
            return cls.get_by_instance_uuid(context, instance['uuid'])

    def to_json(self):
        blob = [{'count': x.count,
                 'spec': x.spec,
                 'alias_name': x.alias_name,
                 'is_new': x.is_new,
                 'numa_policy': x.numa_policy,
                 'request_id': x.request_id,
                 'requester_id': x.requester_id} for x in self.requests]
        return jsonutils.dumps(blob)

    def neutron_requests(self):
        return all(
            [
                req
                for req in self.requests
                if req.source == InstancePCIRequest.NEUTRON_PORT
            ]
        )

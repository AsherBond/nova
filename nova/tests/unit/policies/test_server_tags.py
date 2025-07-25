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

from unittest import mock

import fixtures
from oslo_utils.fixture import uuidsentinel as uuids

from nova.api.openstack.compute import server_tags
from nova.compute import vm_states
from nova import context
from nova import objects
from nova.policies import server_tags as policies
from nova.tests.unit.api.openstack import fakes
from nova.tests.unit import fake_instance
from nova.tests.unit.policies import base


class ServerTagsPolicyTest(base.BasePolicyTest):
    """Test Server Tags APIs policies with all possible context.
    This class defines the set of context with different roles
    which are allowed and not allowed to pass the policy checks.
    With those set of context, it will call the API operation and
    verify the expected behaviour.
    """

    def setUp(self):
        super(ServerTagsPolicyTest, self).setUp()
        self.controller = server_tags.ServerTagsController()
        self.req = fakes.HTTPRequest.blank('', version='2.26')
        self.mock_get = self.useFixture(
            fixtures.MockPatch('nova.api.openstack.common.get_instance')).mock
        self.instance = fake_instance.fake_instance_obj(
            self.project_member_context,
            id=1, uuid=uuids.fake_id, vm_state=vm_states.ACTIVE,
            project_id=self.project_id)
        self.mock_get.return_value = self.instance
        inst_map = objects.InstanceMapping(
            project_id=self.project_id,
            cell_mapping=objects.CellMappingList.get_all(
                context.get_admin_context())[1])
        self.stub_out('nova.objects.InstanceMapping.get_by_instance_uuid',
                      lambda s, c, u: inst_map)

        # With legacy rule and no scope checks, all admin, project members
        # project reader or other project role(because legacy rule allow server
        # owner- having same project id and no role check) is able to perform,
        # operations on server tags.
        self.project_member_authorized_contexts = [
            self.legacy_admin_context, self.system_admin_context,
            self.project_admin_context, self.project_manager_context,
            self.project_member_context, self.project_reader_context,
            self.project_foo_context]
        self.project_reader_authorized_contexts = (
            self.project_member_authorized_contexts)

    @mock.patch('nova.objects.TagList.get_by_resource_id')
    def test_index_server_tags_policy(self, mock_tag):
        rule_name = policies.POLICY_ROOT % 'index'
        self.common_policy_auth(self.project_reader_authorized_contexts,
                                rule_name,
                                self.controller.index,
                                self.req, self.instance.uuid)

    @mock.patch('nova.objects.Tag.exists')
    def test_show_server_tags_policy(self, mock_exists):
        rule_name = policies.POLICY_ROOT % 'show'
        self.common_policy_auth(self.project_reader_authorized_contexts,
                                rule_name,
                                self.controller.show,
                                self.req, self.instance.uuid, uuids.fake_id)

    @mock.patch('nova.notifications.base.send_instance_update_notification')
    @mock.patch('nova.db.main.api.instance_tag_get_by_instance_uuid')
    @mock.patch('nova.objects.Tag.create')
    def test_update_server_tags_policy(self, mock_create, mock_tag,
        mock_notf):
        rule_name = policies.POLICY_ROOT % 'update'
        self.common_policy_auth(self.project_member_authorized_contexts,
                                rule_name,
                                self.controller.update,
                                self.req, self.instance.uuid, uuids.fake_id,
                                body=None)

    @mock.patch('nova.notifications.base.send_instance_update_notification')
    @mock.patch('nova.db.main.api.instance_tag_set')
    def test_update_all_server_tags_policy(self, mock_set, mock_notf):
        rule_name = policies.POLICY_ROOT % 'update_all'
        self.common_policy_auth(self.project_member_authorized_contexts,
                                rule_name,
                                self.controller.update_all,
                                self.req, self.instance.uuid,
                                body={'tags': ['tag1', 'tag2']})

    @mock.patch('nova.notifications.base.send_instance_update_notification')
    @mock.patch('nova.objects.TagList.destroy')
    def test_delete_all_server_tags_policy(self, mock_destroy, mock_notf):
        rule_name = policies.POLICY_ROOT % 'delete_all'
        self.common_policy_auth(self.project_member_authorized_contexts,
                                rule_name,
                                self.controller.delete_all,
                                self.req, self.instance.uuid)

    @mock.patch('nova.notifications.base.send_instance_update_notification')
    @mock.patch('nova.db.main.api.instance_tag_get_by_instance_uuid')
    @mock.patch('nova.objects.Tag.destroy')
    def test_delete_server_tags_policy(self, mock_destroy, mock_get,
        mock_notf):
        rule_name = policies.POLICY_ROOT % 'delete'
        self.common_policy_auth(self.project_member_authorized_contexts,
                                rule_name,
                                self.controller.delete,
                                self.req, self.instance.uuid, uuids.fake_id)


class ServerTagsNoLegacyNoScopePolicyTest(ServerTagsPolicyTest):
    """Test Server Tags APIs policies with no legacy deprecated rules
    and no scope checks.

    """

    without_deprecated_rules = True

    def setUp(self):
        super(ServerTagsNoLegacyNoScopePolicyTest, self).setUp()
        # With no legacy rule, legacy admin loose power.
        self.project_member_authorized_contexts = (
            self.project_member_or_admin_with_no_scope_no_legacy)
        self.project_reader_authorized_contexts = (
            self.project_reader_or_admin_with_no_scope_no_legacy)


class ServerTagsScopeTypePolicyTest(ServerTagsPolicyTest):
    """Test Server Tags APIs policies with system scope enabled.
    This class set the nova.conf [oslo_policy] enforce_scope to True
    so that we can switch on the scope checking on oslo policy side.
    It defines the set of context with scoped token
    which are allowed and not allowed to pass the policy checks.
    With those set of context, it will run the API operation and
    verify the expected behaviour.
    """

    def setUp(self):
        super(ServerTagsScopeTypePolicyTest, self).setUp()
        self.flags(enforce_scope=True, group="oslo_policy")
        # With Scope enable, system users no longer allowed.
        self.project_member_authorized_contexts = (
            self.project_m_r_or_admin_with_scope_and_legacy)
        self.project_reader_authorized_contexts = (
            self.project_m_r_or_admin_with_scope_and_legacy)


class ServerTagsScopeTypeNoLegacyPolicyTest(ServerTagsScopeTypePolicyTest):
    """Test Server Tags APIs policies with system scope enabled,
    and no more deprecated rules that allow the legacy admin API to
    access system APIs.
    """
    without_deprecated_rules = True

    def setUp(self):
        super(ServerTagsScopeTypeNoLegacyPolicyTest, self).setUp()
        # With no legacy and scope enable, only project admin, member,
        # and reader will be able to allowed operation on server tags.
        self.project_member_authorized_contexts = (
            self.project_member_or_admin_with_scope_no_legacy)
        self.project_reader_authorized_contexts = (
            self.project_reader_or_admin_with_scope_no_legacy)

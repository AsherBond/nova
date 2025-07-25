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
from oslo_utils import timeutils

from nova.api.openstack.compute import console_output
from nova.compute import vm_states
from nova.tests.unit.api.openstack import fakes
from nova.tests.unit import fake_instance
from nova.tests.unit.policies import base


class ConsoleOutputPolicyTest(base.BasePolicyTest):
    """Test Console Output APIs policies with all possible context.

    This class defines the set of context with different roles
    which are allowed and not allowed to pass the policy checks.
    With those set of context, it will call the API operation and
    verify the expected behaviour.
    """

    def setUp(self):
        super(ConsoleOutputPolicyTest, self).setUp()
        self.controller = console_output.ConsoleOutputController()
        self.req = fakes.HTTPRequest.blank('')
        self.mock_get = self.useFixture(
            fixtures.MockPatch('nova.api.openstack.common.get_instance')).mock
        uuid = uuids.fake_id
        self.instance = fake_instance.fake_instance_obj(
                self.project_member_context, project_id=self.project_id,
                id=1, uuid=uuid, vm_state=vm_states.ACTIVE,
                task_state=None, launched_at=timeutils.utcnow())
        self.mock_get.return_value = self.instance
        # With legacy rule, any admin and role in project
        # can get the server console.
        self.project_member_authorized_contexts = [
            self.legacy_admin_context, self.system_admin_context,
            self.project_admin_context, self.project_manager_context,
            self.project_member_context, self.project_reader_context,
            self.project_foo_context]

    @mock.patch('nova.compute.api.API.get_console_output')
    def test_console_output_policy(self, mock_console):
        mock_console.return_value = '\n'.join([str(i) for i in range(2)])
        rule_name = "os_compute_api:os-console-output"
        self.common_policy_auth(self.project_member_authorized_contexts,
                                rule_name, self.controller.get_console_output,
                                self.req, self.instance.uuid,
                                body={'os-getConsoleOutput': {}})


class ConsoleOutputNoLegacyNoScopePolicyTest(ConsoleOutputPolicyTest):
    """Test Server Console Output APIs policies with no legacy deprecated
    rule and no scope check.

    """

    without_deprecated_rules = True

    def setUp(self):
        super(ConsoleOutputNoLegacyNoScopePolicyTest, self).setUp()
        # With no legacy rule, only project admin or member is able to
        # get the server console.
        self.project_member_authorized_contexts = (
            self.project_member_or_admin_with_no_scope_no_legacy)


class ConsoleOutputScopeTypePolicyTest(ConsoleOutputPolicyTest):
    """Test Console Output APIs policies with system scope enabled.

    This class set the nova.conf [oslo_policy] enforce_scope to True
    so that we can switch on the scope checking on oslo policy side.
    It defines the set of context with scoped token
    which are allowed and not allowed to pass the policy checks.
    With those set of context, it will run the API operation and
    verify the expected behaviour.
    """

    def setUp(self):
        super(ConsoleOutputScopeTypePolicyTest, self).setUp()
        self.flags(enforce_scope=True, group="oslo_policy")
        # Scope enable will not allow system admin.
        self.project_member_authorized_contexts = (
            self.project_m_r_or_admin_with_scope_and_legacy)


class ConsoleOutputScopeTypeNoLegacyPolicyTest(
        ConsoleOutputScopeTypePolicyTest):
    """Test Console Output APIs policies with system scope enabled,
    and no more deprecated rules.
    """
    without_deprecated_rules = True

    def setUp(self):
        super(ConsoleOutputScopeTypeNoLegacyPolicyTest, self).setUp()

        # With scope enable and no legacy rule, only project admin/member can
        # get the server console.
        self.project_member_authorized_contexts = (
            self.project_member_or_admin_with_scope_no_legacy)

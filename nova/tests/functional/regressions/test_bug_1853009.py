# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import mock

from nova import context
from nova import objects
from nova.tests.functional import integrated_helpers


class NodeRebalanceDeletedComputeNodeRaceTestCase(
        integrated_helpers.ProviderUsageBaseTestCase):
    """Regression test for bug 1853009 observed in Rocky & later.

    When an ironic node re-balances from one host to another, there can be a
    race where the old host deletes the orphan compute node after the new host
    has taken ownership of it which results in the new host failing to create
    the compute node and resource provider because the ResourceTracker does not
    detect a change.
    """
    # Make sure we're using the fake driver that has predictable uuids
    # for each node.
    compute_driver = 'fake.PredictableNodeUUIDDriver'

    def _assert_hypervisor_api(self, nodename, expected_host):
        # We should have one compute node shown by the API.
        hypervisors = self.api.api_get(
            '/os-hypervisors/detail').body['hypervisors']
        self.assertEqual(1, len(hypervisors), hypervisors)
        hypervisor = hypervisors[0]
        self.assertEqual(nodename, hypervisor['hypervisor_hostname'])
        self.assertEqual(expected_host, hypervisor['service']['host'])

    def _start_compute(self, host):
        host = self.start_service('compute', host)
        # Ironic compute driver has rebalances_nodes = True.
        host.manager.driver.rebalances_nodes = True
        return host

    def setUp(self):
        super(NodeRebalanceDeletedComputeNodeRaceTestCase, self).setUp()
        nodename = 'fake-node'
        ctxt = context.get_admin_context()

        # Simulate a service running and then stopping.
        # host2 runs, creates fake-node, then is stopped. The fake-node compute
        # node is destroyed. This leaves a soft-deleted node in the DB.
        host2 = self._start_compute('host2')
        host2.manager.driver._set_nodes([nodename])
        host2.manager.update_available_resource(ctxt)
        host2.stop()
        cn = objects.ComputeNode.get_by_host_and_nodename(
            ctxt, 'host2', nodename)
        cn.destroy()

    def test_node_rebalance_deleted_compute_node_race(self):
        nodename = 'fake-node'
        ctxt = context.get_admin_context()

        # First we create a compute service to manage our node.
        # When start_service runs, it will create a host1 ComputeNode. We want
        # to delete that and inject our fake node into the driver which will
        # be re-balanced to another host later.
        host1 = self._start_compute('host1')
        host1.manager.driver._set_nodes([nodename])

        # Run the update_available_resource periodic to register fake-node and
        # have it managed by host1. This will also detect the "host1" node as
        # orphaned and delete it along with its resource provider.

        # host1[1]: Finds no compute record in RT. Tries to create one
        # (_init_compute_node).
        # FIXME(mgoddard): This shows a traceback with SQL rollback due to
        # soft-deleted node. The create seems to succeed but breaks the RT
        # update for this node. See
        # https://bugs.launchpad.net/nova/+bug/1853159.
        host1.manager.update_available_resource(ctxt)
        self._assert_hypervisor_api(nodename, 'host1')
        # There should only be one resource provider (fake-node).
        original_rps = self._get_all_providers()
        self.assertEqual(1, len(original_rps), original_rps)
        self.assertEqual(nodename, original_rps[0]['name'])

        # Simulate a re-balance by starting host2 and make it manage fake-node.
        # At this point both host1 and host2 think they own fake-node.
        host2 = self._start_compute('host2')
        host2.manager.driver._set_nodes([nodename])

        # host2[1]: Finds no compute record in RT, 'moves' existing node from
        # host1
        host2.manager.update_available_resource(ctxt)
        # Assert that fake-node was re-balanced from host1 to host2.
        self.assertIn('ComputeNode fake-node moving from host1 to host2',
                      self.stdlog.logger.output)
        self._assert_hypervisor_api(nodename, 'host2')

        # host2[2]: Begins periodic update, queries compute nodes for this
        # host, finds the fake-node.
        cn = objects.ComputeNode.get_by_host_and_nodename(
            ctxt, 'host2', nodename)

        # host1[2]: Finds no compute record in RT, 'moves' existing node from
        # host2
        host1.manager.update_available_resource(ctxt)
        # Assert that fake-node was re-balanced from host2 to host1.
        self.assertIn('ComputeNode fake-node moving from host2 to host1',
                      self.stdlog.logger.output)
        self._assert_hypervisor_api(nodename, 'host1')

        # Complete rebalance, as host2 realises it does not own fake-node.
        host2.manager.driver._set_nodes([])

        # host2[2]: Deletes orphan compute node.
        # Mock out the compute node query to simulate a race condition where
        # the list includes an orphan compute node that is taken ownership of
        # by host1 by the time host2 deletes it.
        with mock.patch('nova.compute.manager.ComputeManager.'
                        '_get_compute_nodes_in_db') as mock_get:
            mock_get.return_value = [cn]
            host2.manager.update_available_resource(ctxt)

        # Verify that the node was almost deleted, but saved by the host check.
        self.assertIn("Deleting orphan compute node %s hypervisor host "
                      "is fake-node, nodes are" % cn.id,
                      self.stdlog.logger.output)
        self.assertIn("Ignoring failure to delete orphan compute node %s on "
                      "hypervisor host fake-node" % cn.id,
                      self.stdlog.logger.output)
        self._assert_hypervisor_api(nodename, 'host1')
        rps = self._get_all_providers()
        self.assertEqual(1, len(rps), rps)
        self.assertEqual(nodename, rps[0]['name'])

        # Simulate deletion of an orphan by host2. It shouldn't happen anymore,
        # but let's assume it already did.
        cn = objects.ComputeNode.get_by_host_and_nodename(
            ctxt, 'host1', nodename)
        cn.destroy()
        host2.manager.rt.remove_node(cn.hypervisor_hostname)
        host2.manager.reportclient.delete_resource_provider(
            ctxt, cn, cascade=True)

        # host1[3]: Should recreate compute node and resource provider.
        # FIXME(mgoddard): Resource provider not recreated here, due to
        # https://bugs.launchpad.net/nova/+bug/1853159.
        host1.manager.update_available_resource(ctxt)

        # Verify that the node was recreated.
        self._assert_hypervisor_api(nodename, 'host1')

        # But due to https://bugs.launchpad.net/nova/+bug/1853159 the compute
        # node is not cached in the RT.
        self.assertNotIn(nodename, host1.manager.rt.compute_nodes)

        # There is no RP.
        rps = self._get_all_providers()
        self.assertEqual(0, len(rps), rps)

        # But the RP it exists in the provider tree.
        self.assertFalse(host1.manager.rt.reportclient._provider_tree.exists(
            nodename))

        # host1[1]: Should add compute node to RT cache and recreate resource
        # provider.
        host1.manager.update_available_resource(ctxt)

        # Verify that the node still exists.
        self._assert_hypervisor_api(nodename, 'host1')

        # And it is now in the RT cache.
        self.assertIn(nodename, host1.manager.rt.compute_nodes)

        # The resource provider has now been created.
        rps = self._get_all_providers()
        self.assertEqual(1, len(rps), rps)
        self.assertEqual(nodename, rps[0]['name'])

        # This fails due to the lack of a resource provider.
        self.assertIn('Skipping removal of allocations for deleted instances',
                      self.stdlog.logger.output)

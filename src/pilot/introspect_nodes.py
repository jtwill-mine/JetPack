#!/usr/bin/python

# Copyright (c) 2016-2017 Dell Inc. or its subsidiaries.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import logging
import os
import sys
from arg_helper import ArgHelper
from credential_helper import CredentialHelper
from ironic_helper import IronicHelper
from logging_helper import LoggingHelper
from time import sleep
from update_ssh_config import get_nodes

common_path = os.path.join(os.path.expanduser('~'), 'common')
sys.path.append(common_path)

from thread_helper import ThreadWithExHandling

logging.basicConfig()
logger = logging.getLogger(os.path.splitext(os.path.basename(sys.argv[0]))[0])


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Introspects the overcloud nodes.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    ArgHelper.add_inband_arg(parser)
    LoggingHelper.add_argument(parser)

    return parser.parse_args()


def is_introspection_oob(in_band, node, logger):
    out_of_band = True

    if in_band:
        # All drivers support in-band introspection
        out_of_band = False
    elif node.driver == "pxe_ipmitool":
        # Can't do in-band introspection with the IPMI driver
        logger.warn("The Ironic IPMI driver does not support out-of-band "
                    "introspection.  Using in-band introspection")
        out_of_band = False

    return out_of_band


def get_nodes(ironic_client):
    return ironic_client.node.list(fields=["driver",
                                           "driver_info",
                                           "properties",
                                           "provision_state",
                                           "uuid"])


def refresh_nodes(ironic_client, nodes):
    node_uuids = [node.uuid for node in nodes]

    tmp_nodes = get_nodes(ironic_client)
    nodes = []
    for tmp_node in tmp_nodes:
        if tmp_node.uuid in node_uuids:
            nodes.append(tmp_node)

    return nodes


def transition_to_state(ironic_client, nodes, transition,
                        target_provision_state):
    # Grab a new copy of the nodes to get the current provision_state
    nodes = refresh_nodes(ironic_client, nodes)

    for node in nodes:
        if node.provision_state == target_provision_state:
            logger.debug("Node {} ({}) is already in the {} state".format(
                CredentialHelper.get_drac_ip(node),
                node.uuid,
                target_provision_state))
        else:
            logger.debug("Transitioning node {} ({}) into the {} "
                         "state".format(CredentialHelper.get_drac_ip(node),
                                        node.uuid, target_provision_state))
            ironic_client.node.set_provision_state(node.uuid, transition)

    while True:
        all_nodes_transitioned = True
        nodes = refresh_nodes(ironic_client, nodes)
        for node in nodes:
            if node.provision_state != target_provision_state:
                logger.debug("Node {} ({}) is still in the {} "
                             "state".format(
                                 CredentialHelper.get_drac_ip(node),
                                 node.uuid,
                                 node.provision_state))
                all_nodes_transitioned = False

        if all_nodes_transitioned:
            break
        else:
            sleep(1)

    return nodes


def ib_introspect(node):
    command = "openstack overcloud node introspect " + node.uuid
    if os.system(command) == 0:
        logger.info("In-Band introspection succeeded on node {} ({})".format(
            CredentialHelper.get_drac_ip(node), node.uuid))
    else:
        raise RuntimeError("In-Band introspection failed on node "
                           "{} ({})".format(
                               CredentialHelper.get_drac_ip(node), node.uuid))


def introspect_nodes(in_band, ironic_client, nodes,
                     transition_nodes=True):
    # Check to see if provisioning_mac has been set on all the nodes
    bad_nodes = []
    for node in nodes:
        if "provisioning_mac" not in node.properties:
            bad_nodes.append(node)

    if bad_nodes:
        ips = [CredentialHelper.get_drac_ip(node) for node in bad_nodes]
        fail_msg = "\n".join(ips)

        logger.error("Run config_idrac.py on {} before running "
                     "introspection".format(fail_msg))

    if transition_nodes:
        nodes = transition_to_state(ironic_client, nodes,
                                    'manage', 'manageable')

    threads = []
    bad_nodes = []
    for node in nodes:
        use_oob_introspection = is_introspection_oob(in_band, node, logger)

        introspection_type = "out-of-band"
        if not use_oob_introspection:
            introspection_type = "in-band"

        logger.info("Starting {} introspection on node "
                    "{} ({})".format(introspection_type,
                                     CredentialHelper.get_drac_ip(node),
                                     node.uuid))

        if not use_oob_introspection:
            thread = ThreadWithExHandling(logger,
                                          object_identity=node,
                                          target=ib_introspect,
                                          args=(node,))
            threads.append(thread)
            thread.start()
        else:
            if os.system("openstack baremetal node inspect " + node.uuid) != 0:
                bad_nodes.append(node)

    for thread in threads:
        thread.join()

    for thread in threads:
        if thread.ex is not None:
            bad_nodes.append(thread.object_identity)

    if bad_nodes:
        ips = ["{} ({})".format(CredentialHelper.get_drac_ip(node),
                                node.uuid) for node in bad_nodes]
        raise RuntimeError("Failed to introspect {}".format(", ".join(ips)))

    if not use_oob_introspection:
        # The PERC H740P RAID controller only makes virtual disks visible to
        # the host OS.  Physical disks are not visible with this controller
        # because it does not support pass-through mode.  This results in
        # local_gb not being set during IB introspection, which causes
        # problems further along in the flow.

        # Check to see if all nodes have local_gb defined, and if not run OOB
        # introspection to discover local_gb.
        nodes = refresh_nodes(ironic_client, nodes)
        bad_nodes = []
        for node in nodes:
            if 'local_gb' not in node.properties:
                bad_nodes.append(node)

        if bad_nodes:
            ips = [CredentialHelper.get_drac_ip(node) for node in bad_nodes]
            fail_msg = "\n".join(ips)

            logger.info("local_gb was not discovered on:  {}".format(fail_msg))

            logger.info("Running OOB introspection to populate it.")
            introspect_nodes(False, ironic_client, bad_nodes,
                             transition_nodes=False)

    if transition_nodes:
        nodes = transition_to_state(ironic_client, nodes,
                                    'provide', 'available')

    if use_oob_introspection:
        # FIXME: Remove this hack when OOB introspection is fixed
        for node in nodes:
            delete_non_pxe_ports(ironic_client, node)


def delete_non_pxe_ports(ironic_client, node):
    ip = CredentialHelper.get_drac_ip(node)

    logger.info("Deleting all non-PXE ports from node {} ({})...".format(
        ip, node.uuid))

    for port in ironic_client.node.list_ports(node.uuid):
        if port.address.lower() != \
                node.properties["provisioning_mac"].lower():
            logger.info("Deleting port {} ({}) {}".format(
                ip, node.uuid, port.address.lower()))
            ironic_client.port.delete(port.uuid)


def main():
    args = parse_arguments()

    LoggingHelper.configure_logging(args.logging_level)

    ironic_client = IronicHelper.get_ironic_client()
    nodes = get_nodes(ironic_client)

    introspect_nodes(args.in_band, ironic_client, nodes)


if __name__ == "__main__":
    main()

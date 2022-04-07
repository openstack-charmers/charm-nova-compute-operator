#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#

import logging
import platform

from typing import Callable
from typing import List

from lightkube import ApiError, Client
from lightkube.resources.apps_v1 import StatefulSet
from lightkube.types import PatchType

import ops

from ops.framework import StoredState
from ops.main import main

import advanced_sunbeam_openstack.charm as sunbeam_charm
import advanced_sunbeam_openstack.core as sunbeam_core
import advanced_sunbeam_openstack.container_handlers as sunbeam_chandlers
import advanced_sunbeam_openstack.config_contexts as sunbeam_contexts
import advanced_sunbeam_openstack.relation_handlers as sunbeam_rhandlers

import charms.sunbeam_nova_compute_operator.v0.cloud_compute as cloud_compute
import charms.sunbeam_keystone_operator.v0.cloud_credentials as \
    cloud_credentials

logger = logging.getLogger(__name__)


class NovaComputePebbleHandler(sunbeam_chandlers.ServicePebbleHandler):

    def get_layer(self):
        """Nova Compute Service

        :returns: pebble layer configuration for nova compute service
        :rtype: dict
        """
        return {
            "summary": "nova compute layer",
            "description": "pebble configuration for nova compute service",
            "services": {
                "libvirtd": {
                    "override": "replace",
                    "summary": "libvirtd",
                    "command": "/usr/sbin/libvirtd",
                    "startup": "enabled",
                },
                "nova-compute": {
                    "override": "replace",
                    "summary": "Nova Compute",
                    "command": "nova-compute",
                    "startup": "enabled",
                },
            }
        }

    def default_container_configs(self):
        # TODO(wolsen) fill in the necessary config contexts
        return [
            sunbeam_core.ContainerConfigFile(
                '/etc/nova/nova.conf',
                'nova',
                'nova'),
        ]


class CloudCredentialsRequiresHandler(sunbeam_rhandlers.RelationHandler):
    """Handles the cloud credentials relation on the requires side."""

    def __init__(
        self,
        charm: ops.charm.CharmBase,
        relation_name: str,
        callback_f: Callable,
    ):
        """Creates a new CloudCredentialsRequiresHandler that handles initial
        events from the relation and invokes the provided callbacks based on
        the event raised.

        :param charm: the Charm class the handler is for
        :type charm: ops.charm.CharmBase
        :param relation_name: the relation the handler is bound to
        :type relation_name: str
        :param callback_f: the function to call when the nodes are connected
        :type callback_f: Callable
        """
        super().__init__(charm, relation_name, callback_f)

    def setup_event_handler(self) -> ops.charm.Object:
        """

        """
        logger.debug('Setting up the cloud-credentials event handler')
        credentials_service = cloud_credentials.CloudCredentialsRequires(
            self.charm, self.relation_name,
        )
        self.framework.observe(
            credentials_service.on.ready,
            self._credentials_ready
        )
        return credentials_service

    def _credentials_ready(self, event):
        """

        """
        self.callback_f(event)

    @property
    def ready(self) -> bool:
        return True


class CloudComputeProvidesHandler(sunbeam_rhandlers.RelationHandler):
    """Handles the cloud-compute relation on the provides side."""

    def __init__(
        self,
        charm: ops.charm.CharmBase,
        relation_name: str,
        hostname: str,
        availability_zone: str,
        callback_f: Callable,
    ):
        """Creates a new CloudComputeRequiresHandler that handles initial
        events from the relation and invokes the provided callbacks based on
        the event raised.

        :param charm: the Charm class the handler is for
        :type charm: ops.charm.CharmBase
        :param relation_name: the relation the handler is bound to
        :type relation_name: str
        :param callback_f: the function to call when the nodes are connected
        :type callback_f: Callable
        """
        self.hostname = hostname
        self.availability_zone = availability_zone
        super().__init__(charm, relation_name, callback_f)

    def setup_event_handler(self) -> ops.charm.Object:
        """Configure event handlers for the cloud-compute service relation."""
        logger.debug('Setting up cloud-compute event handler')
        compute_service = cloud_compute.CloudComputeProvides(
            self.charm,
            self.relation_name,
        )
        self.framework.observe(
            compute_service.on.has_cloud_compute_clients,
            self._controller_nodes_connected
        )
        self.framework.observe(
            compute_service.on.ready_cloud_compute_clients,
            self._controller_nodes_ready
        )
        return compute_service

    def _controller_nodes_connected(self, event) -> None:
        """Handles cloud-compute connected events."""
        self.interface.set_compute_node_info(
            event.relation_name,
            event.relation_id,
            self.hostname,
            self.availability_zone,
        )

    def _controller_nodes_ready(self, event) -> None:
        """Handles cloud-compute ready events."""
        # Ready is only emitted when the interface considers
        # that the relation is complete (indicated by an availability zone)
        self.callback_f(event)

    @property
    def ready(self) -> bool:
        return True


class NovaComputeContext(sunbeam_contexts.ConfigContext):
    """Defines context settings for Nova compute service."""

    def context(self) -> dict:
        """

        """
        return {
            'arch': platform.machine(),
            'instances_path': '/var/lib/nova/instances',
        }


class NovaComputeOperatorCharm(sunbeam_charm.OSBaseOperatorCharm):
    """Charm the service."""

    _stored = StoredState()
    service_name = 'nova-compute'
    openstack_release = 'xena'

    def __init__(self, framework):
        """

        """
        super().__init__(framework)
        self._patch_as_needed()

    def _patch_as_needed(self):
        """Patches the Kubernetes PodSpec for the application to ensure it
        has the necessary tweaks for running nova-compute.

        This will only patch the service if there are services that local
        patches that are required but not applied.
        """
        if not self.unit.is_leader():
            logger.debug('Letting leader unit determine if patching is '
                         'necessary.')
            return

        # Note, we could treat this as a podspec charm and use
        # self.model.pod.set_spec... And we probably should for this particular
        # scenario, but need to determine how the pod-spec bits work here.
        # They are deprecated and split between ones that Juju understands and
        # ones that Juju doesn't.
        #
        # Also note that the pod security policies tweaked herein are
        # deprecated for removal in k8s 1.25. We should migrate to Pod
        # Security Admission.
        client = Client()
        sf_set = client.get(StatefulSet, self.model.app.name,
                            namespace=self._namespace())
        pod_spec = sf_set.spec.template.spec
        needs_patching = False
        if not pod_spec.hostNetwork:
            pod_spec.hostNetwork = True
            needs_patching = True

        if pod_spec.dnsPolicy != 'ClusterFirstWithHostNet':
            pod_spec.dnsPolicy = 'ClusterFirstWithHostNet'
            needs_patching = True

        if not needs_patching:
            logger.debug('No need to patch the StatefulSet. It matches our '
                         'desired spec')
            return

        logger.debug('Patching StatefulSet')
        try:
            client.patch(StatefulSet, self.model.app.name, sf_set,
                         patch_type=PatchType.MERGE)
        except ApiError:
            logger.exception('Error patching stateful set.')

    def _namespace(self):
        """Returns the namespace (aka juju model) for the application"""
        """The Kubernetes namespace we're running in.

        Returns:
            str: A string containing the name of the current Kubernetes namespace.
        """
        ns_file = '/var/run/secrets/kubernetes.io/serviceaccount/namespace'
        with open(ns_file, 'r') as f:
            return f.read().strip()

    def get_pebble_handlers(self) -> List[sunbeam_chandlers.PebbleHandler]:
        """Returns the available pebble handlers the nova compute charm.

        :returns: a List of PebbleHandlers
        :rtype: List[sunbeam_chandlers.PebbleHandler]
        """
        return [
            NovaComputePebbleHandler(
                self,
                'nova-compute',
                self.service_name,
                self.container_configs,
                self.template_dir,
                self.openstack_release,
                self.configure_charm,
            )
        ]

    def get_relation_handlers(
        self, handlers: List[sunbeam_rhandlers.RelationHandler] = None
    ) -> List[sunbeam_rhandlers.RelationHandler]:
        """

        """
        handlers = [
            CloudComputeProvidesHandler(
                self,
                'cloud-compute',
                'my-host.example.org',
                self.model.config['default-availability-zone'],
                self.configure_charm,
            ), CloudCredentialsRequiresHandler(
                self,
                'cloud-credentials',
                self.configure_charm,
            )
        ]
        return super().get_relation_handlers(handlers)

    def configure_charm(self, event: ops.framework.EventBase) -> None:
        """

        """
        logger.info('Configuring charm')
        super().configure_charm(event)


if __name__ == "__main__":
    main(NovaComputeOperatorCharm, use_juju_for_storage=True)

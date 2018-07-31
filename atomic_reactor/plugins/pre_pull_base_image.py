"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Pull parent image(s) the build will use, enforcing that they can only come from
the specified registry.
If this build is an auto-rebuild, use the base image from the image change
trigger instead of what is in the Dockerfile.
Tag each image to a unique name (the build name plus a nonce) to be used during
this build so that it isn't removed by other builds doing clean-up.
"""

from __future__ import unicode_literals

import docker
import platform

from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.util import (get_build_json, get_manifest_list,
                                 get_config_from_registry, ImageName,
                                 get_platforms)
from atomic_reactor.core import RetryGeneratorException
from atomic_reactor.plugins.pre_reactor_config import (get_source_registry,
                                                       get_platform_to_goarch_mapping,
                                                       get_goarch_to_platform_mapping)
from requests.exceptions import HTTPError, RetryError, Timeout
from osbs.utils import RegistryURI


class PullBaseImagePlugin(PreBuildPlugin):
    key = "pull_base_image"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, parent_registry=None, parent_registry_insecure=False,
                 check_platforms=False):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param parent_registry: registry to enforce pulling from
        :param parent_registry_insecure: allow connecting to the registry over plain http
        :param check_platforms: validate parent images provide all platforms expected for the build
        """
        # call parent constructor
        super(PullBaseImagePlugin, self).__init__(tasker, workflow)

        self.check_platforms = check_platforms
        source_registry = get_source_registry(self.workflow, {
            'uri': RegistryURI(parent_registry) if parent_registry else None,
            'insecure': parent_registry_insecure})

        if source_registry.get('uri'):
            self.parent_registry = source_registry['uri'].docker_uri
            self.parent_registry_insecure = source_registry['insecure']
        else:
            self.parent_registry = None
            self.parent_registry_insecure = False

    def run(self):
        """
        Pull parent images and retag them uniquely for this build.
        """
        build_json = get_build_json()
        base_image_str = str(self.workflow.builder.original_base_image)
        current_platform = platform.processor() or 'x86_64'
        self.manifest_list_cache = {}
        for nonce, parent in enumerate(sorted(self.workflow.builder.parent_images.keys())):
            image = ImageName.parse(parent)
            if parent == base_image_str:
                image = self._resolve_base_image(build_json)
            image = self._ensure_image_registry(image)

            if self.check_platforms:
                self._validate_platforms_in_image(image)

                new_arch_image = self._get_image_for_different_arch(image, current_platform)
                if new_arch_image:
                    image = new_arch_image

            new_image = self._pull_and_tag_image(image, build_json, str(nonce))
            self.workflow.builder.parent_images[parent] = str(new_image)

            if parent == base_image_str:
                self.workflow.builder.set_base_image(str(new_image))

    def _get_image_for_different_arch(self, image, platform):
        manifest_list = self._get_manifest_list(image)
        new_image = None

        if manifest_list:
            manifest_list_dict = manifest_list.json()
            arch_digests = {}
            build_image_digests = {}
            image_name = image.to_str(tag=False)

            for manifest in manifest_list_dict['manifests']:
                arch = manifest['platform']['architecture']
                arch_digests[arch] = image_name + '@' + manifest['digest']

            present_platform = None
            try:
                arch_to_platform = get_goarch_to_platform_mapping(self.workflow)
                for arch, digest in arch_digests.items():
                    present_platform = arch_to_platform[arch]
                    build_image_digests[present_platform] = digest

                if platform not in build_image_digests:
                    new_image = ImageName.parse(build_image_digests[present_platform])
            except KeyError:
                self.log.info('Cannot validate available platforms for base image '
                              'because platform descriptors are not defined')
        return new_image

    def _resolve_base_image(self, build_json):
        """If this is an auto-rebuild, adjust the base image to use the triggering build"""
        spec = build_json.get("spec")
        try:
            image_id = spec['triggeredBy'][0]['imageChangeBuild']['imageID']
        except (TypeError, KeyError, IndexError):
            # build not marked for auto-rebuilds; use regular base image
            base_image = self.workflow.builder.base_image
            self.log.info("using %s as base image.", base_image)
        else:
            # build has auto-rebuilds enabled
            self.log.info("using %s from build spec[triggeredBy] as base image.", image_id)
            base_image = ImageName.parse(image_id)  # any exceptions will propagate

        return base_image

    def _ensure_image_registry(self, image):
        """If plugin configured with a parent registry, ensure the image uses it"""
        image_with_registry = image.copy()
        if self.parent_registry:
            # if registry specified in Dockerfile image, ensure it's the one allowed by config
            if image.registry and image.registry != self.parent_registry:
                error = (
                    "Registry specified in dockerfile image doesn't match configured one. "
                    "Dockerfile: '%s'; expected registry: '%s'"
                    % (image, self.parent_registry))
                self.log.error("%s", error)
                raise RuntimeError(error)

            image_with_registry.registry = self.parent_registry

        return image_with_registry

    def _pull_and_tag_image(self, image, build_json, nonce):
        """Docker pull the image and tag it uniquely for use by this build"""
        image = image.copy()
        first_library_exc = None
        for _ in range(20):
            # retry until pull and tag is successful or definitively fails.
            # should never require 20 retries but there's a race condition at work.
            # just in case something goes wildly wrong, limit to 20 so it terminates.
            try:
                self.tasker.pull_image(image, insecure=self.parent_registry_insecure)
                self.workflow.pulled_base_images.add(image.to_str())
            except RetryGeneratorException as exc:
                # getting here means the pull itself failed. we may want to retry if the
                # image being pulled lacks a namespace, like e.g. "rhel7". we cannot count
                # on the registry mapping this into the docker standard "library/rhel7" so
                # need to retry with that.
                if first_library_exc:
                    # we already tried and failed; report the first failure.
                    raise first_library_exc
                if image.namespace:
                    # already namespaced, do not retry with "library/", just fail.
                    raise

                self.log.info("'%s' not found", image.to_str())
                image.namespace = 'library'
                self.log.info("trying '%s'", image.to_str())
                first_library_exc = exc  # report first failure if retry also fails
                continue

            # Attempt to tag it using a unique ID. We might have to retry
            # if another build with the same parent image is finishing up
            # and removing images it pulled.

            # Use the OpenShift build name as the unique ID
            unique_id = build_json['metadata']['name']
            new_image = ImageName(repo=unique_id, tag=nonce)

            try:
                self.log.info("tagging pulled image")
                response = self.tasker.tag_image(image, new_image)
                self.workflow.pulled_base_images.add(response)
                self.log.debug("image '%s' is available as '%s'", image, new_image)
                return new_image
            except docker.errors.NotFound:
                # If we get here, some other build raced us to remove
                # the parent image, and that build won.
                # Retry the pull immediately.
                self.log.info("re-pulling removed image")
                continue

        # Failed to tag it after 20 tries
        self.log.error("giving up trying to pull image")
        raise RuntimeError("too many attempts to pull and tag image")

    def _get_manifest_list(self, image):
        """try to figure out manifest list"""
        if image in self.manifest_list_cache:
            return self.manifest_list_cache[image]

        manifest_list = get_manifest_list(image, image.registry,
                                          insecure=self.parent_registry_insecure)
        if '@sha256:' in str(image) and not manifest_list:
            # we want to adjust the tag only for manifest list fetching
            image = image.copy()

            try:
                config_blob = get_config_from_registry(image, image.registry, image.tag,
                                                       insecure=self.parent_registry_insecure)
            except (HTTPError, RetryError, Timeout) as ex:
                self.log.warning('Unable to fetch config for %s, got error %s',
                                 image, ex.response.status_code)
                raise RuntimeError('Unable to fetch config for base image')

            release = config_blob['config']['Labels']['release']
            version = config_blob['config']['Labels']['version']
            docker_tag = "%s-%s" % (version, release)
            image.tag = docker_tag

            manifest_list = get_manifest_list(image, image.registry,
                                              insecure=self.parent_registry_insecure)
        self.manifest_list_cache[image] = manifest_list
        return self.manifest_list_cache[image]

    def _validate_platforms_in_image(self, image):
        """Ensure that the image provides all platforms expected for the build."""
        expected_platforms = get_platforms(self.workflow)
        if not expected_platforms:
            self.log.info('Skipping validation of available platforms '
                          'because expected platforms are unknown')
            return
        if len(expected_platforms) == 1:
            self.log.info('Skipping validation of available platforms for base image '
                          'because this is a single platform build')
            return

        if not image.registry:
            self.log.info('Cannot validate available platforms for base image '
                          'because base image registry is not defined')
            return

        try:
            platform_to_arch = get_platform_to_goarch_mapping(self.workflow)
        except KeyError:
            self.log.info('Cannot validate available platforms for base image '
                          'because platform descriptors are not defined')
            return

        manifest_list = self._get_manifest_list(image)

        if not manifest_list:
            raise RuntimeError('Unable to fetch manifest list for base image')

        all_manifests = manifest_list.json()['manifests']
        manifest_list_arches = set(
            manifest['platform']['architecture'] for manifest in all_manifests)

        expected_arches = set(
            platform_to_arch[platform] for platform in expected_platforms)

        self.log.info('Manifest list arches: %s, expected arches: %s',
                      manifest_list_arches, expected_arches)
        assert manifest_list_arches >= expected_arches, \
            'Missing arches in manifest list for base image'

        self.log.info('Base image is a manifest list for all required platforms')

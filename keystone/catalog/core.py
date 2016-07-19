# Copyright 2012 OpenStack Foundation
# Copyright 2012 Canonical Ltd.
#
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

"""Main entry point into the Catalog service."""

import abc
import itertools

from oslo_cache import core as oslo_cache
from oslo_config import cfg
from oslo_log import log
import six

from keystone.common import cache
from keystone.common import dependency
from keystone.common import driver_hints
from keystone.common import manager
from keystone.common import utils
from keystone import exception
from keystone.i18n import _
from keystone.i18n import _LE
from keystone import notifications


CONF = cfg.CONF
LOG = log.getLogger(__name__)
WHITELISTED_PROPERTIES = [
    'tenant_id', 'project_id', 'user_id',
    'public_bind_host', 'admin_bind_host',
    'compute_host', 'admin_port', 'public_port',
    'public_endpoint', 'admin_endpoint', ]

# This is a general cache region for catalog administration (CRUD operations).
MEMOIZE = cache.get_memoization_decorator(group='catalog')

# This builds a discrete cache region dedicated to complete service catalogs
# computed for a given user + project pair. Any write operation to create,
# modify or delete elements of the service catalog should invalidate this
# entire cache region.
COMPUTED_CATALOG_REGION = oslo_cache.create_region()
MEMOIZE_COMPUTED_CATALOG = cache.get_memoization_decorator(
    group='catalog',
    region=COMPUTED_CATALOG_REGION)


def format_url(url, substitutions, silent_keyerror_failures=None):
    """Formats a user-defined URL with the given substitutions.

    :param string url: the URL to be formatted
    :param dict substitutions: the dictionary used for substitution
    :param list silent_keyerror_failures: keys for which we should be silent
        if there is a KeyError exception on substitution attempt
    :returns: a formatted URL

    """
    substitutions = utils.WhiteListedItemFilter(
        WHITELISTED_PROPERTIES,
        substitutions)
    allow_keyerror = silent_keyerror_failures or []
    try:
        result = url.replace('$(', '%(') % substitutions
    except AttributeError:
        LOG.error(_LE('Malformed endpoint - %(url)r is not a string'),
                  {"url": url})
        raise exception.MalformedEndpoint(endpoint=url)
    except KeyError as e:
        if not e.args or e.args[0] not in allow_keyerror:
            LOG.error(_LE("Malformed endpoint %(url)s - unknown key "
                          "%(keyerror)s"),
                      {"url": url,
                       "keyerror": e})
            raise exception.MalformedEndpoint(endpoint=url)
        else:
            result = None
    except TypeError as e:
        LOG.error(_LE("Malformed endpoint '%(url)s'. The following type error "
                      "occurred during string substitution: %(typeerror)s"),
                  {"url": url,
                   "typeerror": e})
        raise exception.MalformedEndpoint(endpoint=url)
    except ValueError as e:
        LOG.error(_LE("Malformed endpoint %s - incomplete format "
                      "(are you missing a type notifier ?)"), url)
        raise exception.MalformedEndpoint(endpoint=url)
    return result


def check_endpoint_url(url):
    """Check substitution of url.

    The invalid urls are as follows:
    urls with substitutions that is not in the whitelist

    Check the substitutions in the URL to make sure they are valid
    and on the whitelist.

    :param str url: the URL to validate
    :rtype: None
    :raises keystone.exception.URLValidationError: if the URL is invalid
    """
    # check whether the property in the path is exactly the same
    # with that in the whitelist below
    substitutions = dict(zip(WHITELISTED_PROPERTIES, itertools.repeat('')))
    try:
        url.replace('$(', '%(') % substitutions
    except (KeyError, TypeError, ValueError):
        raise exception.URLValidationError(url)


@dependency.provider('catalog_api')
@dependency.requires('resource_api')
class Manager(manager.Manager):
    """Default pivot point for the Catalog backend.

    See :mod:`keystone.common.manager.Manager` for more details on how this
    dynamically calls the backend.

    """

    driver_namespace = 'keystone.catalog'

    _ENDPOINT = 'endpoint'
    _SERVICE = 'service'
    _REGION = 'region'

    def __init__(self):
        super(Manager, self).__init__(CONF.catalog.driver)

    def create_region(self, region_ref, initiator=None):
        # Check duplicate ID
        try:
            self.get_region(region_ref['id'])
        except exception.RegionNotFound:  # nosec
            # A region with the same id doesn't exist already, good.
            pass
        else:
            msg = _('Duplicate ID, %s.') % region_ref['id']
            raise exception.Conflict(type='region', details=msg)

        # NOTE(lbragstad,dstanek): The description column of the region
        # database cannot be null. So if the user doesn't pass in a
        # description or passes in a null description then set it to an
        # empty string.
        if region_ref.get('description') is None:
            region_ref['description'] = ''
        try:
            ret = self.driver.create_region(region_ref)
        except exception.NotFound:
            parent_region_id = region_ref.get('parent_region_id')
            raise exception.RegionNotFound(region_id=parent_region_id)

        notifications.Audit.created(self._REGION, ret['id'], initiator)
        COMPUTED_CATALOG_REGION.invalidate()
        return ret

    @MEMOIZE
    def get_region(self, region_id):
        try:
            return self.driver.get_region(region_id)
        except exception.NotFound:
            raise exception.RegionNotFound(region_id=region_id)

    def update_region(self, region_id, region_ref, initiator=None):
        # NOTE(lbragstad,dstanek): The description column of the region
        # database cannot be null. So if the user passes in a null
        # description set it to an empty string.
        if 'description' in region_ref and region_ref['description'] is None:
            region_ref['description'] = ''
        ref = self.driver.update_region(region_id, region_ref)
        notifications.Audit.updated(self._REGION, region_id, initiator)
        self.get_region.invalidate(self, region_id)
        COMPUTED_CATALOG_REGION.invalidate()
        return ref

    def delete_region(self, region_id, initiator=None):
        try:
            ret = self.driver.delete_region(region_id)
            notifications.Audit.deleted(self._REGION, region_id, initiator)
            self.get_region.invalidate(self, region_id)
            COMPUTED_CATALOG_REGION.invalidate()
            return ret
        except exception.NotFound:
            raise exception.RegionNotFound(region_id=region_id)

    @manager.response_truncated
    def list_regions(self, hints=None):
        return self.driver.list_regions(hints or driver_hints.Hints())

    def create_service(self, service_id, service_ref, initiator=None):
        service_ref.setdefault('enabled', True)
        service_ref.setdefault('name', '')
        ref = self.driver.create_service(service_id, service_ref)
        notifications.Audit.created(self._SERVICE, service_id, initiator)
        COMPUTED_CATALOG_REGION.invalidate()
        return ref

    @MEMOIZE
    def get_service(self, service_id):
        try:
            return self.driver.get_service(service_id)
        except exception.NotFound:
            raise exception.ServiceNotFound(service_id=service_id)

    def update_service(self, service_id, service_ref, initiator=None):
        ref = self.driver.update_service(service_id, service_ref)
        notifications.Audit.updated(self._SERVICE, service_id, initiator)
        self.get_service.invalidate(self, service_id)
        COMPUTED_CATALOG_REGION.invalidate()
        return ref

    def delete_service(self, service_id, initiator=None):
        try:
            endpoints = self.list_endpoints()
            ret = self.driver.delete_service(service_id)
            notifications.Audit.deleted(self._SERVICE, service_id, initiator)
            self.get_service.invalidate(self, service_id)
            for endpoint in endpoints:
                if endpoint['service_id'] == service_id:
                    self.get_endpoint.invalidate(self, endpoint['id'])
            COMPUTED_CATALOG_REGION.invalidate()
            return ret
        except exception.NotFound:
            raise exception.ServiceNotFound(service_id=service_id)

    @manager.response_truncated
    def list_services(self, hints=None):
        return self.driver.list_services(hints or driver_hints.Hints())

    def _assert_region_exists(self, region_id):
        try:
            if region_id is not None:
                self.get_region(region_id)
        except exception.RegionNotFound:
            raise exception.ValidationError(attribute='endpoint region_id',
                                            target='region table')

    def _assert_service_exists(self, service_id):
        try:
            if service_id is not None:
                self.get_service(service_id)
        except exception.ServiceNotFound:
            raise exception.ValidationError(attribute='endpoint service_id',
                                            target='service table')

    def create_endpoint(self, endpoint_id, endpoint_ref, initiator=None):
        self._assert_region_exists(endpoint_ref.get('region_id'))
        self._assert_service_exists(endpoint_ref['service_id'])
        ref = self.driver.create_endpoint(endpoint_id, endpoint_ref)

        notifications.Audit.created(self._ENDPOINT, endpoint_id, initiator)
        COMPUTED_CATALOG_REGION.invalidate()
        return ref

    def update_endpoint(self, endpoint_id, endpoint_ref, initiator=None):
        self._assert_region_exists(endpoint_ref.get('region_id'))
        self._assert_service_exists(endpoint_ref.get('service_id'))
        ref = self.driver.update_endpoint(endpoint_id, endpoint_ref)
        notifications.Audit.updated(self._ENDPOINT, endpoint_id, initiator)
        self.get_endpoint.invalidate(self, endpoint_id)
        COMPUTED_CATALOG_REGION.invalidate()
        return ref

    def delete_endpoint(self, endpoint_id, initiator=None):
        try:
            ret = self.driver.delete_endpoint(endpoint_id)
            notifications.Audit.deleted(self._ENDPOINT, endpoint_id, initiator)
            self.get_endpoint.invalidate(self, endpoint_id)
            COMPUTED_CATALOG_REGION.invalidate()
            return ret
        except exception.NotFound:
            raise exception.EndpointNotFound(endpoint_id=endpoint_id)

    @MEMOIZE
    def get_endpoint(self, endpoint_id):
        try:
            return self.driver.get_endpoint(endpoint_id)
        except exception.NotFound:
            raise exception.EndpointNotFound(endpoint_id=endpoint_id)

    @manager.response_truncated
    def list_endpoints(self, hints=None):
        return self.driver.list_endpoints(hints or driver_hints.Hints())

    @MEMOIZE_COMPUTED_CATALOG
    def get_catalog(self, user_id, tenant_id):
        try:
            return self.driver.get_catalog(user_id, tenant_id)
        except exception.NotFound:
            raise exception.NotFound('Catalog not found for user and tenant')

    @MEMOIZE_COMPUTED_CATALOG
    def get_v3_catalog(self, user_id, tenant_id):
        return self.driver.get_v3_catalog(user_id, tenant_id)

    def add_endpoint_to_project(self, endpoint_id, project_id):
        self.driver.add_endpoint_to_project(endpoint_id, project_id)
        COMPUTED_CATALOG_REGION.invalidate()

    def remove_endpoint_from_project(self, endpoint_id, project_id):
        self.driver.remove_endpoint_from_project(endpoint_id, project_id)
        COMPUTED_CATALOG_REGION.invalidate()

    def add_endpoint_group_to_project(self, endpoint_group_id, project_id):
        self.driver.add_endpoint_group_to_project(
            endpoint_group_id, project_id)
        COMPUTED_CATALOG_REGION.invalidate()

    def remove_endpoint_group_from_project(self, endpoint_group_id,
                                           project_id):
        self.driver.remove_endpoint_group_from_project(
            endpoint_group_id, project_id)
        COMPUTED_CATALOG_REGION.invalidate()

    def get_endpoint_groups_for_project(self, project_id):
        # recover the project endpoint group memberships and for each
        # membership recover the endpoint group
        self.resource_api.get_project(project_id)
        try:
            refs = self.list_endpoint_groups_for_project(project_id)
            endpoint_groups = [self.get_endpoint_group(
                ref['endpoint_group_id']) for ref in refs]
            return endpoint_groups
        except exception.EndpointGroupNotFound:
            return []

    def get_endpoints_filtered_by_endpoint_group(self, endpoint_group_id):
        endpoints = self.list_endpoints()
        filters = self.get_endpoint_group(endpoint_group_id)['filters']
        filtered_endpoints = []

        for endpoint in endpoints:
            is_candidate = True
            for key, value in filters.items():
                if endpoint[key] != value:
                    is_candidate = False
                    break
            if is_candidate:
                filtered_endpoints.append(endpoint)
        return filtered_endpoints

    def list_endpoints_for_project(self, project_id):
        """List all endpoints associated with a project.

        :param project_id: project identifier to check
        :type project_id: string
        :returns: a list of endpoint ids or an empty list.

        """
        refs = self.driver.list_endpoints_for_project(project_id)
        filtered_endpoints = {}
        for ref in refs:
            try:
                endpoint = self.get_endpoint(ref['endpoint_id'])
                filtered_endpoints.update({ref['endpoint_id']: endpoint})
            except exception.EndpointNotFound:
                # remove bad reference from association
                self.remove_endpoint_from_project(ref['endpoint_id'],
                                                  project_id)

        # need to recover endpoint_groups associated with project
        # then for each endpoint group return the endpoints.
        endpoint_groups = self.get_endpoint_groups_for_project(project_id)
        for endpoint_group in endpoint_groups:
            endpoint_refs = self.get_endpoints_filtered_by_endpoint_group(
                endpoint_group['id'])
            # now check if any endpoints for current endpoint group are not
            # contained in the list of filtered endpoints
            for endpoint_ref in endpoint_refs:
                if endpoint_ref['id'] not in filtered_endpoints:
                    filtered_endpoints[endpoint_ref['id']] = endpoint_ref

        return filtered_endpoints


@six.add_metaclass(abc.ABCMeta)
class CatalogDriverV8(object):
    """Interface description for the Catalog driver."""

    def _get_list_limit(self):
        return CONF.catalog.list_limit or CONF.list_limit

    def _ensure_no_circle_in_hierarchical_regions(self, region_ref):
        if region_ref.get('parent_region_id') is None:
            return

        root_region_id = region_ref['id']
        parent_region_id = region_ref['parent_region_id']

        while parent_region_id:
            # NOTE(wanghong): check before getting parent region can ensure no
            # self circle
            if parent_region_id == root_region_id:
                raise exception.CircularRegionHierarchyError(
                    parent_region_id=parent_region_id)
            parent_region = self.get_region(parent_region_id)
            parent_region_id = parent_region.get('parent_region_id')

    @abc.abstractmethod
    def create_region(self, region_ref):
        """Creates a new region.

        :raises keystone.exception.Conflict: If the region already exists.
        :raises keystone.exception.RegionNotFound: If the parent region
            is invalid.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_regions(self, hints):
        """List all regions.

        :param hints: contains the list of filters yet to be satisfied.
                      Any filters satisfied here will be removed so that
                      the caller will know if any filters remain.

        :returns: list of region_refs or an empty list.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def get_region(self, region_id):
        """Get region by id.

        :returns: region_ref dict
        :raises keystone.exception.RegionNotFound: If the region doesn't exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def update_region(self, region_id, region_ref):
        """Update region by id.

        :returns: region_ref dict
        :raises keystone.exception.RegionNotFound: If the region doesn't exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def delete_region(self, region_id):
        """Deletes an existing region.

        :raises keystone.exception.RegionNotFound: If the region doesn't exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def create_service(self, service_id, service_ref):
        """Creates a new service.

        :raises keystone.exception.Conflict: If a duplicate service exists.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_services(self, hints):
        """List all services.

        :param hints: contains the list of filters yet to be satisfied.
                      Any filters satisfied here will be removed so that
                      the caller will know if any filters remain.

        :returns: list of service_refs or an empty list.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def get_service(self, service_id):
        """Get service by id.

        :returns: service_ref dict
        :raises keystone.exception.ServiceNotFound: If the service doesn't
            exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def update_service(self, service_id, service_ref):
        """Update service by id.

        :returns: service_ref dict
        :raises keystone.exception.ServiceNotFound: If the service doesn't
            exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def delete_service(self, service_id):
        """Deletes an existing service.

        :raises keystone.exception.ServiceNotFound: If the service doesn't
            exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def create_endpoint(self, endpoint_id, endpoint_ref):
        """Creates a new endpoint for a service.

        :raises keystone.exception.Conflict: If a duplicate endpoint exists.
        :raises keystone.exception.ServiceNotFound: If the service doesn't
            exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def get_endpoint(self, endpoint_id):
        """Get endpoint by id.

        :returns: endpoint_ref dict
        :raises keystone.exception.EndpointNotFound: If the endpoint doesn't
            exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_endpoints(self, hints):
        """List all endpoints.

        :param hints: contains the list of filters yet to be satisfied.
                      Any filters satisfied here will be removed so that
                      the caller will know if any filters remain.

        :returns: list of endpoint_refs or an empty list.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def update_endpoint(self, endpoint_id, endpoint_ref):
        """Get endpoint by id.

        :returns: endpoint_ref dict
        :raises keystone.exception.EndpointNotFound: If the endpoint doesn't
            exist.
        :raises keystone.exception.ServiceNotFound: If the service doesn't
            exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def delete_endpoint(self, endpoint_id):
        """Deletes an endpoint for a service.

        :raises keystone.exception.EndpointNotFound: If the endpoint doesn't
            exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def get_catalog(self, user_id, tenant_id):
        """Retrieve and format the current service catalog.

        Example::

            { 'RegionOne':
                {'compute': {
                    'adminURL': u'http://host:8774/v1.1/tenantid',
                    'internalURL': u'http://host:8774/v1.1/tenant_id',
                    'name': 'Compute Service',
                    'publicURL': u'http://host:8774/v1.1/tenantid'},
                 'ec2': {
                    'adminURL': 'http://host:8773/services/Admin',
                    'internalURL': 'http://host:8773/services/Cloud',
                    'name': 'EC2 Service',
                    'publicURL': 'http://host:8773/services/Cloud'}}

        :returns: A nested dict representing the service catalog or an
                  empty dict.
        :raises keystone.exception.NotFound: If the endpoint doesn't exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    def get_v3_catalog(self, user_id, tenant_id):
        """Retrieve and format the current V3 service catalog.

        The default implementation builds the V3 catalog from the V2 catalog.

        Example::

            [
                {
                    "endpoints": [
                    {
                        "interface": "public",
                        "id": "--endpoint-id--",
                        "region": "RegionOne",
                        "url": "http://external:8776/v1/--project-id--"
                    },
                    {
                        "interface": "internal",
                        "id": "--endpoint-id--",
                        "region": "RegionOne",
                        "url": "http://internal:8776/v1/--project-id--"
                    }],
                "id": "--service-id--",
                "type": "volume"
            }]

        :returns: A list representing the service catalog or an empty list
        :raises keystone.exception.NotFound: If the endpoint doesn't exist.

        """
        v2_catalog = self.get_catalog(user_id, tenant_id)
        v3_catalog = []

        for region_name, region in v2_catalog.items():
            for service_type, service in region.items():
                service_v3 = {
                    'type': service_type,
                    'endpoints': []
                }

                for attr, value in service.items():
                    # Attributes that end in URL are interfaces. In the V2
                    # catalog, these are internalURL, publicURL, and adminURL.
                    # For example, <region_name>.publicURL=<URL> in the V2
                    # catalog becomes the V3 interface for the service:
                    # { 'interface': 'public', 'url': '<URL>', 'region':
                    #   'region: '<region_name>' }
                    if attr.endswith('URL'):
                        v3_interface = attr[:-len('URL')]
                        service_v3['endpoints'].append({
                            'interface': v3_interface,
                            'region': region_name,
                            'url': value,
                        })
                        continue

                    # Other attributes are copied to the service.
                    service_v3[attr] = value

                v3_catalog.append(service_v3)

        return v3_catalog

    @abc.abstractmethod
    def add_endpoint_to_project(self, endpoint_id, project_id):
        """Create an endpoint to project association.

        :param endpoint_id: identity of endpoint to associate
        :type endpoint_id: string
        :param project_id: identity of the project to be associated with
        :type project_id: string
        :raises: keystone.exception.Conflict: If the endpoint was already
            added to project.
        :returns: None.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def remove_endpoint_from_project(self, endpoint_id, project_id):
        """Removes an endpoint to project association.

        :param endpoint_id: identity of endpoint to remove
        :type endpoint_id: string
        :param project_id: identity of the project associated with
        :type project_id: string
        :raises keystone.exception.NotFound: If the endpoint was not found
            in the project.
        :returns: None.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def check_endpoint_in_project(self, endpoint_id, project_id):
        """Checks if an endpoint is associated with a project.

        :param endpoint_id: identity of endpoint to check
        :type endpoint_id: string
        :param project_id: identity of the project associated with
        :type project_id: string
        :raises keystone.exception.NotFound: If the endpoint was not found
            in the project.
        :returns: None.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_endpoints_for_project(self, project_id):
        """List all endpoints associated with a project.

        :param project_id: identity of the project to check
        :type project_id: string
        :returns: a list of identity endpoint ids or an empty list.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_projects_for_endpoint(self, endpoint_id):
        """List all projects associated with an endpoint.

        :param endpoint_id: identity of endpoint to check
        :type endpoint_id: string
        :returns: a list of projects or an empty list.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def delete_association_by_endpoint(self, endpoint_id):
        """Removes all the endpoints to project association with endpoint.

        :param endpoint_id: identity of endpoint to check
        :type endpoint_id: string
        :returns: None

        """
        raise exception.NotImplemented()

    @abc.abstractmethod
    def delete_association_by_project(self, project_id):
        """Removes all the endpoints to project association with project.

        :param project_id: identity of the project to check
        :type project_id: string
        :returns: None

        """
        raise exception.NotImplemented()

    @abc.abstractmethod
    def create_endpoint_group(self, endpoint_group):
        """Create an endpoint group.

        :param endpoint_group: endpoint group to create
        :type endpoint_group: dictionary
        :raises: keystone.exception.Conflict: If a duplicate endpoint group
            already exists.
        :returns: an endpoint group representation.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def get_endpoint_group(self, endpoint_group_id):
        """Get an endpoint group.

        :param endpoint_group_id: identity of endpoint group to retrieve
        :type endpoint_group_id: string
        :raises keystone.exception.NotFound: If the endpoint group was not
            found.
        :returns: an endpoint group representation.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def update_endpoint_group(self, endpoint_group_id, endpoint_group):
        """Update an endpoint group.

        :param endpoint_group_id: identity of endpoint group to retrieve
        :type endpoint_group_id: string
        :param endpoint_group: A full or partial endpoint_group
        :type endpoint_group: dictionary
        :raises keystone.exception.NotFound: If the endpoint group was not
            found.
        :returns: an endpoint group representation.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def delete_endpoint_group(self, endpoint_group_id):
        """Delete an endpoint group.

        :param endpoint_group_id: identity of endpoint group to delete
        :type endpoint_group_id: string
        :raises keystone.exception.NotFound: If the endpoint group was not
            found.
        :returns: None.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def add_endpoint_group_to_project(self, endpoint_group_id, project_id):
        """Adds an endpoint group to project association.

        :param endpoint_group_id: identity of endpoint to associate
        :type endpoint_group_id: string
        :param project_id: identity of project to associate
        :type project_id: string
        :raises keystone.exception.Conflict: If the endpoint group was already
            added to the project.
        :returns: None.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def get_endpoint_group_in_project(self, endpoint_group_id, project_id):
        """Get endpoint group to project association.

        :param endpoint_group_id: identity of endpoint group to retrieve
        :type endpoint_group_id: string
        :param project_id: identity of project to associate
        :type project_id: string
        :raises keystone.exception.NotFound: If the endpoint group to the
            project association was not found.
        :returns: a project endpoint group representation.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_endpoint_groups(self):
        """List all endpoint groups.

        :returns: None.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_endpoint_groups_for_project(self, project_id):
        """List all endpoint group to project associations for a project.

        :param project_id: identity of project to associate
        :type project_id: string
        :returns: None.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_projects_associated_with_endpoint_group(self, endpoint_group_id):
        """List all projects associated with endpoint group.

        :param endpoint_group_id: identity of endpoint to associate
        :type endpoint_group_id: string
        :returns: None.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def remove_endpoint_group_from_project(self, endpoint_group_id,
                                           project_id):
        """Remove an endpoint to project association.

        :param endpoint_group_id: identity of endpoint to associate
        :type endpoint_group_id: string
        :param project_id: identity of project to associate
        :type project_id: string
        :raises keystone.exception.NotFound: If endpoint group project
            association was not found.
        :returns: None.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def delete_endpoint_group_association_by_project(self, project_id):
        """Remove endpoint group to project associations.

        :param project_id: identity of the project to check
        :type project_id: string
        :returns: None

        """
        raise exception.NotImplemented()  # pragma: no cover

Driver = manager.create_legacy_driver(CatalogDriverV8)

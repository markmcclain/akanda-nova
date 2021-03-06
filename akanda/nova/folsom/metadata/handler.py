# vim: tabstop=4 shiftwidth=4 softtabstop=4

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

"""Metadata request handler."""
import hashlib
import hmac
import os

import webob.dec
import webob.exc

from nova import exception
from nova import flags
from nova.openstack.common import cfg
from nova.openstack.common import log as logging
from nova import wsgi

from . import base

CACHE_EXPIRATION = 15  # in seconds

FLAGS = flags.FLAGS
flags.DECLARE('memcached_servers', 'nova.flags')
flags.DECLARE('use_forwarded_for', 'nova.api.auth')

metadata_proxy_opts = [
    cfg.BoolOpt(
        'service_quantum_metadata_proxy',
        default=False,
        help='Set flag to indicate Quantum will proxy metadata requests and '
        'resolve instance ids.'),
    cfg.StrOpt(
        'quantum_metadata_proxy_shared_secret',
        default='',
        help='Shared secret to validate proxies Quantum metadata requests')
]

FLAGS.register_opts(metadata_proxy_opts)

LOG = logging.getLogger(__name__)

if FLAGS.memcached_servers:
    import memcache
else:
    from nova.common import memorycache as memcache


class MetadataRequestHandler(wsgi.Application):
    """Serve metadata."""

    def __init__(self):
        self._cache = memcache.Client(FLAGS.memcached_servers, debug=0)

    def get_metadata_by_remote_address(self, address):
        if not address:
            raise exception.FixedIpNotFoundForAddress(address=address)

        cache_key = 'metadata-%s' % address
        data = self._cache.get(cache_key)
        if data:
            return data

        try:
            data = base.get_metadata_by_address(address)
        except exception.NotFound:
            return None

        self._cache.set(cache_key, data, CACHE_EXPIRATION)

        return data

    def get_metadata_by_instance_id(self, instance_id, address):
        cache_key = 'metadata-%s' % instance_id
        data = self._cache.get(cache_key)
        if data:
            return data

        try:
            data = base.get_metadata_by_instance_id(instance_id, address)
        except exception.NotFound:
            return None

        self._cache.set(cache_key, data, CACHE_EXPIRATION)

        return data

    @webob.dec.wsgify(RequestClass=wsgi.Request)
    def __call__(self, req):
        if os.path.normpath("/" + req.path_info) == "/":
            return(base.ec2_md_print(base.VERSIONS + ["latest"]))

        if FLAGS.service_quantum_metadata_proxy:
            meta_data = self._handle_instance_id_request(req)
        else:
            if req.headers.get('X-Instance-ID'):
                LOG.warn(
                    _("X-Instance-ID present in request headers. The "
                      "'service_quantum_metadata_proxy' option must be enabled"
                      " to process this header."))
            meta_data = self._handle_remote_ip_request(req)

        if meta_data is None:
            raise webob.exc.HTTPNotFound()

        try:
            data = meta_data.lookup(req.path_info)
        except base.InvalidMetadataPath:
            raise webob.exc.HTTPNotFound()

        if callable(data):
            return data(req, meta_data)

        return base.ec2_md_print(data)

    def _handle_remote_ip_request(self, req):
        remote_address = req.remote_addr
        if FLAGS.use_forwarded_for:
            remote_address = req.headers.get('X-Forwarded-For', remote_address)

        try:
            meta_data = self.get_metadata_by_remote_address(remote_address)
        except Exception:
            LOG.exception(_('Failed to get metadata for ip: %s'),
                          remote_address)
            msg = _('An unknown error has occurred. '
                    'Please try your request again.')
            raise webob.exc.HTTPInternalServerError(explanation=unicode(msg))

        if meta_data is None:
            LOG.error(_('Failed to get metadata for ip: %s'), remote_address)

        return meta_data

    def _handle_instance_id_request(self, req):
        instance_id = req.headers.get('X-Instance-ID')
        signature = req.headers.get('X-Instance-ID-Signature')
        remote_address = req.headers.get('X-Forwarded-For')

        # Ensure that only one header was passed

        if instance_id is None:
            msg = _('X-Instance-ID header is missing from request.')
        elif not isinstance(instance_id, basestring):
            msg = _('Multiple X-Instance-ID headers found within request.')
        else:
            msg = None

        if msg:
            raise webob.exc.HTTPBadRequest(explanation=msg)

        expected_signature = hmac.new(
            FLAGS.quantum_metadata_proxy_shared_secret,
            instance_id,
            hashlib.sha256).hexdigest()

        if expected_signature != signature:
            if instance_id:
                w = _('X-Instance-ID-Signature: %(signature)s does not match '
                      'the expected value: %(expected_signature)s for id: '
                      '%(instance_id)s.  Request From: %(remote_address)s')
                LOG.warn(w % locals())

            msg = _('Invalid proxy request signature.')
            raise webob.exc.HTTPForbidden(explanation=msg)

        try:
            meta_data = self.get_metadata_by_instance_id(instance_id,
                                                         remote_address)
        except Exception:
            LOG.exception(_('Failed to get metadata for instance id: %s'),
                          instance_id)
            msg = _('An unknown error has occurred. '
                    'Please try your request again.')
            raise webob.exc.HTTPInternalServerError(explanation=unicode(msg))

        if meta_data is None:
            LOG.error(_('Failed to get metadata for instance id: %s'),
                      instance_id)

        return meta_data

# -*- coding: utf-8 -*-
import json

from odoo import http
from odoo.http import request


class MazenetIncomingApi(http.Controller):

    @http.route('/api/mazenet/<path:route>', type='http', auth='public',
                methods=['POST'], csrf=False, save_session=False)
    def incoming_api(self, route, **kwargs):
        """ Incoming APIs configured in API Configuration (route + the server's bearer token). """
        payload, status = request.env['api.configuration'].sudo()._handle_incoming(
            route,
            dict(request.httprequest.headers),
            request.httprequest.get_data(as_text=True),
            request.httprequest.url)
        return request.make_response(
            json.dumps(payload), headers=[('Content-Type', 'application/json')], status=status)

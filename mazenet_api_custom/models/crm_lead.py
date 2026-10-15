# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import UserError


class CrmLead(models.Model):
    _inherit = 'crm.lead'

    # work_order_id = fields.Integer(string="Work Order ID", readonly=True, copy=False)
    maze_query_id = fields.Integer(string="Work Order ID", readonly=True, copy=False)
    maze_wo_no = fields.Integer(string="Work Order ID", readonly=True, copy=False)
    work_order_url = fields.Char(string="Work Order URL", readonly=True, copy=False)
    work_start_date = fields.Date(string="Work Start Date", readonly=True, copy=False)
    work_end_date = fields.Date(string="Work End Date", readonly=True, copy=False)

    def action_create_work_order(self):
        """ Create the work order in the other system (API configuration 'Create work Order'). """
        self.ensure_one()
        # if self.work_order_id:
        #     raise UserError(_("A work order is already created for this lead."))
        if self.maze_query_id or self.maze_wo_no:
            raise UserError(_("A work order is already created for this lead."))
        if not (self.skill_type_id and self.business_service_id):
            raise UserError(_("Select the Skill and the Business Service first."))
        # the API expects the email of the Mazenet user creating the work order
        # email = (self.user_id.email or self.env.user.email or '').strip()
        email = (self.email_from or'').strip()
        if not email:
            raise UserError(_("The salesperson has no email."))
        config = self.env['api.configuration'].sudo().search(
            [('api_type', '=', 'create_work_order')], limit=1)
        if not config:
            raise UserError(_("Create an API Configuration with API Identification 'Create work Order'."))
        payload = {
            'bu_id': self.skill_type_id.bu_id,
            'service_id': self.business_service_id.maze_id,
            'email': email,
        }

        def store_work_order(answer):
            data = answer.get('data') if isinstance(answer, dict) else None
            if not data or not data.get('id'):
                raise ValueError(_("The answer has no work order id."))
            self.write({'maze_query_id': data['query_id'],'maze_wo_no': data['wo_no'],
             'work_order_url': data.get('url') or False})

        log = config._call_api(payload=payload, on_success=store_work_order)
        failed = log.status == 'failed'
        if failed:
            # a notification (not an exception) so the failed call stays in the API log
            return {
                'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': _("Work order failed"), 'message': log.error_message,
                           'type': 'danger', 'sticky': True},
            }
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _("Work order"), 'message': _("Work order created."),
                       'type': 'success', 'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'}},
        }

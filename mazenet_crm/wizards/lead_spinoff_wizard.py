# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class MzLeadSpinoffWizard(models.TransientModel):
    _name = "mz.lead.spinoff.wizard"
    _description = "Create New Lead From Existing Lead (Spin-off)"

    source_lead_id = fields.Many2one(
        "crm.lead", string="Source Lead", required=True,
        help="The lead this new one is being spun off from - same customer, a "
             "different requirement (e.g. they bought chairs, now they also want "
             "a computer)."
    )
    name = fields.Char(string="Opportunity", required=True)
    partner_id = fields.Many2one("res.partner", string="Customer")
    partner_name = fields.Char(string="Company Name")
    contact_name = fields.Char(string="Contact Name")
    email_from = fields.Char(string="Email")
    phone = fields.Char(string="Phone")
    team_id = fields.Many2one("crm.team", string="Sales Team", required=True)
    assignable_user_ids = fields.Many2many(
        "res.users", compute="_compute_assignable_user_ids",
        help="team_id.member_ids - used as user_id's domain in the view. A plain "
             "dotted domain ('team_id.member_ids.ids') can't be evaluated "
             "client-side since the wizard's team_id record data doesn't carry "
             "its own member_ids along with it, so this compute exists purely to "
             "give the view a real field to filter on."
    )
    user_id = fields.Many2one(
        "res.users", string="Salesperson",
        domain="[('id', 'in', assignable_user_ids)]",
    )

    @api.depends('team_id')
    def _compute_assignable_user_ids(self):
        for wizard in self:
            wizard.assignable_user_ids = wizard.team_id.member_ids
    description = fields.Text(
        string="New Requirement",
        help="What the customer is asking for THIS time - left blank rather than "
             "copied from the source lead, since it's a new ask, not a repeat of "
             "the old one."
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        lead = self.env['crm.lead'].browse(self.env.context.get('active_id'))
        if lead.exists():
            res.update({
                'source_lead_id': lead.id,
                'name': _("%s - New Requirement") % lead.name,
                'partner_id': lead.partner_id.id,
                'partner_name': lead.partner_name,
                'contact_name': lead.contact_name,
                'email_from': lead.email_from,
                'phone': lead.phone,
            })
        return res

    def action_create_lead(self):
        self.ensure_one()
        first_stage = self.env['crm.stage'].search(
            [('team_ids', 'in', self.team_id.id)], order='sequence asc', limit=1
        )
        if not first_stage:
            raise UserError(_(
                "'%s' has no pipeline stages configured - can't create a lead "
                "for it."
            ) % self.team_id.name)

        new_lead = self.env['crm.lead'].create({
            'name': self.name,
            'type': 'opportunity',
            'partner_id': self.partner_id.id,
            'partner_name': self.partner_name,
            'contact_name': self.contact_name,
            'email_from': self.email_from,
            'phone': self.phone,
            'team_id': self.team_id.id,
            'user_id': self.user_id.id,
            'stage_id': first_stage.id,
            'description': self.description,
            'x_related_lead_id': self.source_lead_id.id,
        })

        self.source_lead_id.message_post(body=_(
            "Spun off a new lead for a different requirement: %(link)s "
            "(Team: %(team)s)."
        ) % {'link': new_lead._get_html_link(), 'team': self.team_id.name})
        new_lead.message_post(body=_(
            "Created from %(link)s - existing customer, new requirement."
        ) % {'link': self.source_lead_id._get_html_link()})

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'crm.lead',
            'res_id': new_lead.id,
            'view_mode': 'form',
            'target': 'current',
        }

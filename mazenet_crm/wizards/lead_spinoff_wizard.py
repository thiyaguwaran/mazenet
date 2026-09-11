# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class MzLeadSpinoffWizard(models.TransientModel):
    _name = "mz.lead.spinoff.wizard"
    _description = "Create New Opportunity From Existing Lead (Spin-off)"

    # Every "detail" field copied from the source lead (client instruction, 2026-09-09:
    # "all details should be pre-filled ... except assigning") - customer/contact/address/
    # marketing-source info, NOT the assignment fields (team_id/user_id, kept blank/manual
    # below - picking who/which team gets the new opportunity is a fresh decision each
    # time, not a copied "detail") and NOT description (still deliberately blank - a
    # spin-off is a NEW requirement, not a repeat of the old one).
    DETAIL_FIELDS = [
        'partner_id', 'partner_name', 'contact_name', 'email_from', 'email_cc', 'phone',
        'function', 'website', 'street', 'street2', 'city', 'state_id', 'zip', 'country_id',
        'tag_ids', 'priority', 'campaign_id', 'medium_id', 'source_id', 'referred',
        'company_id', 'lang_id',
    ]

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
    email_cc = fields.Char(string="Cc")
    phone = fields.Char(string="Phone")
    function = fields.Char(string="Job Position")
    website = fields.Char(string="Website")
    street = fields.Char(string="Street")
    street2 = fields.Char(string="Street 2")
    city = fields.Char(string="City")
    state_id = fields.Many2one("res.country.state", string="State")
    zip = fields.Char(string="ZIP")
    country_id = fields.Many2one("res.country", string="Country")
    tag_ids = fields.Many2many("crm.tag", string="Tags")
    priority = fields.Selection(
        [('0', 'Low'), ('1', 'Medium'), ('2', 'High'), ('3', 'Very High')],
        string="Priority", default='0',
    )
    campaign_id = fields.Many2one("utm.campaign", string="Campaign")
    medium_id = fields.Many2one("utm.medium", string="Medium")
    source_id = fields.Many2one("utm.source", string="Source")
    referred = fields.Char(string="Referred By")
    company_id = fields.Many2one("res.company", string="Company")
    lang_id = fields.Many2one("res.lang", string="Language")
    team_id = fields.Many2one("crm.team", string="Sales Team", required=True)
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
            })
            for fname in self.DETAIL_FIELDS:
                field = lead._fields[fname]
                value = lead[fname]
                if field.type == 'many2many':
                    res[fname] = [(6, 0, value.ids)]
                elif field.type == 'many2one':
                    res[fname] = value.id
                else:
                    res[fname] = value
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

        vals = {
            'name': self.name,
            'type': 'opportunity',
            'team_id': self.team_id.id,
            # No Salesperson field on this wizard (client instruction, 2026-09-11) -
            # same reasoning as DMT+Team on the single-lead form: the spin-off just
            # routes to a team, the receiving team's lead assigns the actual
            # salesperson afterwards. Explicit False needed - crm.lead's own
            # user_id defaults to self.env.user otherwise, silently assigning the
            # NEW opportunity to whoever ran this wizard.
            'user_id': False,
            'stage_id': first_stage.id,
            'description': self.description,
            'x_related_lead_id': self.source_lead_id.id,
        }
        for fname in self.DETAIL_FIELDS:
            field = self._fields[fname]
            value = self[fname]
            if field.type == 'many2many':
                vals[fname] = [(6, 0, value.ids)]
            elif field.type == 'many2one':
                vals[fname] = value.id
            else:
                vals[fname] = value

        # Each team's own ir.rule create-domain restricts creation to leads with
        # team_id = that user's own team (e.g. "Mazenet CRM Lead: dmt (Team View/
        # Create)") - correct for the normal case, but a spin-off is specifically
        # meant to let DMT (and CTO/Admin/MD, same cross-team authority as everywhere
        # else - _mz_user_can_use_assign_radio) route a brand-new opportunity to a
        # DIFFERENT team than their own. Without sudo() here, that legitimate cross-
        # team create hits the SAME AccessError a raw unauthorized cross-team create
        # would (hit live 2026-09-11: DMT Agent blocked creating a Tally-routed
        # opportunity by rule_crm_lead_dmt_base). Anyone else keeps the normal,
        # non-sudo create - if they're not authorized to route outside their own
        # team, the ir.rule should still stop them, same as it always has.
        crm_lead = self.env['crm.lead']
        if crm_lead._mz_user_can_use_assign_radio(self.env.user):
            crm_lead = crm_lead.sudo()
        new_lead = crm_lead.create(vals)

        self.source_lead_id.message_post(body=_(
            "Spun off a new opportunity for a different requirement: %(link)s "
            "(Team: %(team)s)."
        ) % {'link': new_lead._get_html_link(), 'team': self.team_id.name})
        new_lead.message_post(body=_(
            "Created from %(link)s - existing customer, new requirement."
        ) % {'link': self.source_lead_id._get_html_link()})

        # DMT specifically has NO read access outside its own team (mazenet_access_
        # rights' group_mzr_dmt_agent deliberately excludes sales_team.
        # group_sale_salesman_all_leads - "No visibility into other teams") - so
        # redirecting them into the very lead they just (sudo-)created for another
        # team would immediately hit a read AccessError on open. CTO/Admin/MD read
        # every team regardless (own dedicated ir.rules), so they're fine either way.
        user = self.env.user
        can_view_new_lead = (
            user.has_group('mazenet_access_rights.group_mzr_cto_admin')
            or user.has_group('mazenet_access_rights.group_mzr_md')
            or self.team_id == self.env['crm.lead']._mz_user_own_team(user)
        )
        if can_view_new_lead:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'crm.lead',
                'res_id': new_lead.id,
                'view_mode': 'form',
                'target': 'current',
            }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Opportunity Created"),
                'message': _(
                    "New opportunity '%(name)s' created and routed to %(team)s."
                ) % {'name': new_lead.name, 'team': self.team_id.name},
                'type': 'success',
            },
        }

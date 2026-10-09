# -*- coding: utf-8 -*-
from odoo import models, fields


class UtmSource(models.Model):
    _inherit = "utm.source"

    team_ids = fields.Many2many(
        "crm.team", "mazenet_utm_source_team_rel", "source_id", "team_id",
        string="Sales Teams",
        help="Which BU pipelines offer this source as a Lead Source option. "
             "Used to scope crm.lead's source_id domain per team_id "
             "(mazenet_crm's answer to the M2 pipeline sheets' per-BU Source list)."
    )
    x_requires_reference_text = fields.Boolean(
        string="Requires Reference Text",
        help="Whether crm.lead.referred (the free-text companion field) is mandatory "
             "once this source is selected - the M2 sheets' 'Text mandatory if selected' "
             "sources (Referral, Ads, GeM Bid, Partner/Customer Referral...). Checked by "
             "crm_lead.py's per-stage mandatory-field gate; a plain flag here instead of "
             "matching on source name, so it isn't tied to exact wording."
    )

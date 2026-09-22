# -*- coding: utf-8 -*-
from odoo import fields, models, tools


class CrmLeadReport(models.Model):
    _name = 'crm.lead.report'
    _description = 'Lead Report'
    _auto = False
    _order = 'create_date desc'

    user_id = fields.Many2one('res.users', string='Salesperson', readonly=True)
    team_id = fields.Many2one('crm.team', string='Sales Team', readonly=True)
    stage_id = fields.Many2one('crm.stage', string='Stage', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    type = fields.Selection([
        ('lead', 'Lead'), ('opportunity', 'Opportunity')], string='Type', readonly=True)
    priority = fields.Selection([
        ('0', 'Low'), ('1', 'Medium'), ('2', 'High'), ('3', 'Very High')],
        string='Priority', readonly=True)
    active = fields.Boolean(string='Active', readonly=True)
    create_date = fields.Datetime(string='Created On', readonly=True)
    expected_revenue = fields.Monetary(string='Expected Revenue', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW crm_lead_report AS (
                SELECT
                    l.id AS id,
                    l.user_id AS user_id,
                    l.team_id AS team_id,
                    l.stage_id AS stage_id,
                    l.company_id AS company_id,
                    l.type AS type,
                    l.priority AS priority,
                    l.active AS active,
                    l.create_date AS create_date,
                    l.expected_revenue AS expected_revenue,
                    c.currency_id AS currency_id
                FROM crm_lead l
                LEFT JOIN res_company c ON c.id = l.company_id
            )
        """)

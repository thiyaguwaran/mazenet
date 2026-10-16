# -*- coding: utf-8 -*-
from odoo import models, fields,api,_
from odoo.exceptions import ValidationError

class SkillType(models.Model):
    _name = "skill.type"
    _description = "Skill Type"
    _rec_name = "name"

    name = fields.Char(string="Product Name",required=True)
    short_name = fields.Char(string="Short Name",required=True)
    company_id = fields.Many2one(
        'res.company',string="company",required=True,
        default=lambda self: self.env.company,ondelete="cascade")
    # team_id = fields.Many2one(
    #         'crm.team',string="Team",ondelete="set null")
    bu_id = fields.Integer(string="BU ID",required=True)
    business_service_ids = fields.One2many(
        'business.service', 'skill_type_id', string="Business Services")

    def action_open_form(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.display_name,
            'res_model': 'skill.type',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    @api.constrains('name', 'short_name', 'team_id')
    def check_unique(self):
        if self.env.context.get('skip_validation'):
            return
        for rec in self:
            domain = [('id', '!=', rec.id),('team_id', '=', rec.team_id.id),
            '|',
            ('name', '=', rec.name),('short_name', '=', rec.short_name),]
            if self.sudo().search_count(domain):
                raise ValidationError(_(
                    "A record with name '%(name)s' and short name '%(short)s' "
                    "already exists for team '%(team)s'.",name=rec.name,
                    short=rec.short_name,team=rec.team_id.display_name,))




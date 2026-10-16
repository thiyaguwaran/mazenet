# -*- coding: utf-8 -*-
from odoo import models, fields,api,_
from odoo.exceptions import ValidationError

""" !!! note :this model is belong buisiness unit named as skill type 
consider it as buisiness unit!!"""
class SkillType(models.Model):
    _name = "skill.type"
    _description = "Skill Type"
    _rec_name = "name"

    name = fields.Char(string="Product Name",required=True)
    short_name = fields.Char(string="Short Name",required=True)
    company_id = fields.Many2one(
        'res.company',string="company",required=True,
        default=lambda self: self.env.company,ondelete="cascade")
    team_ids = fields.Many2many(
        'crm.team', 'skill_type_crm_team_rel', 'skill_type_id', 'team_id',
        string="Teams")
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

    @api.constrains('name', 'short_name', 'team_ids')
    def check_unique(self):
        """ The same name / short name cannot be used twice for the same team
        (or twice for Buisiness Units that have no team). """
        if self.env.context.get('skip_validation'):
            return
        for rec in self:
            others = self.sudo().search([
                ('id', '!=', rec.id),
                '|', ('name', '=', rec.name), ('short_name', '=', rec.short_name)])
            clash = others.filtered(
                lambda o: (set(o.team_ids.ids) & set(rec.team_ids.ids))
                or (not o.team_ids and not rec.team_ids))
            if clash:
                raise ValidationError(_(
                    "A record with name '%(name)s' and short name '%(short)s' "
                    "already exists for the team(s) '%(team)s'.",
                    name=rec.name, short=rec.short_name,
                    team=', '.join(rec.team_ids.mapped('name')) or _("no team")))

    @api.constrains('team_ids')
    def _check_team_mapped_once(self):
        """ A team can be mapped to one Buisiness Unit only. """
        for rec in self:
            for team in rec.team_ids:
                other = self.sudo().search([
                    ('id', '!=', rec.id), ('team_ids', 'in', team.id)], limit=1)
                if other:
                    raise ValidationError(_(
                        "Team '%(team)s' is already mapped to the Buisiness Unit '%(skill)s'.",
                        team=team.name, skill=other.name))

# -*- coding: utf-8 -*-
from odoo import models, fields


class SkillType(models.Model):
    _name = "skill.type"
    _description = "Skill Type"
    _rec_name = "name"

    name = fields.Char(string="Name",required=True)
    company_id = fields.Many2one(
        'res.company',string="company",required=True,
        default=lambda self: self.env.company)



# -*- coding: utf-8 -*-
from odoo import models, fields


class BusinessService(models.Model):
    _name = "business.service"
    _description = "Business Service"
    _rec_name = "services"

    skill_type_id = fields.Many2one(
        'skill.type', string="Skill Type", required=True, ondelete="cascade")
    maze_id = fields.Integer(string="Maze ID")
    services = fields.Char(string="Services", required=True)
    shortname = fields.Char(string="Short Name")

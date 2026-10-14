# -*- coding: utf-8 -*-
from odoo import api, models


class SkillType(models.Model):
    _inherit = 'skill.type'

    @api.model_create_multi
    def create(self, vals_list):
        skill_types = super().create(vals_list)
        if not self.env.context.get('skip_business_service_fetch'):
            # failures are logged in the API log and never block the skill type creation
            self.env['api.configuration']._fetch_business_services(skill_types)
        return skill_types

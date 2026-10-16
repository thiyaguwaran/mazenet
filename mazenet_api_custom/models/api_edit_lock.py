# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError

# written by the system on every call: never locked
SYSTEM_FIELDS = {'state', 'last_sync'}


class ApiEditLock(models.AbstractModel):
    """ API Configuration and Server Details can only be changed when the company allows it. """
    _name = 'api.edit.lock'
    _description = 'API Edit Lock'

    can_edit_api_config = fields.Boolean(compute='_compute_can_edit_api_config')

    @api.model
    def _api_config_edit_allowed(self):
        return bool(self.env.company.sudo().allow_api_config_edit)

    def _compute_can_edit_api_config(self):
        allowed = self._api_config_edit_allowed()
        for rec in self:
            rec.can_edit_api_config = allowed

    @api.model
    def _check_api_config_edit(self, vals=None):
        if self._api_config_edit_allowed():
            return
        if vals is not None and all(
                f in SYSTEM_FIELDS or f.startswith(('message_', 'activity_')) for f in vals):
            return
        raise UserError(_("Editing the API settings is not allowed for this company. "
                          "Enable 'Allow API Configuration Edit' on the company first."))

    @api.model_create_multi
    def create(self, vals_list):
        self._check_api_config_edit()
        return super().create(vals_list)

    def write(self, vals):
        self._check_api_config_edit(vals)
        return super().write(vals)

    def unlink(self):
        self._check_api_config_edit()
        return super().unlink()

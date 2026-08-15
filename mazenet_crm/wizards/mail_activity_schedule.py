# -*- coding: utf-8 -*-
from odoo import models, fields


class MailActivitySchedule(models.TransientModel):
    _inherit = "mail.activity.schedule"

    mz_activity_time = fields.Float(
        string="Time",
        widget="float_time",
        help="Only used for Call / To-Do activities - Meeting activities get their real "
             "time from the Calendar event created on the next step instead."
    )

    def _action_schedule_activities(self):
        activities = super(MailActivitySchedule, self)._action_schedule_activities()
        activities.mz_activity_time = self.mz_activity_time
        return activities

    def _action_schedule_activities_personal(self):
        activity = super(MailActivitySchedule, self)._action_schedule_activities_personal()
        activity.mz_activity_time = self.mz_activity_time
        return activity

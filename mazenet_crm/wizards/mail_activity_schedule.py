# -*- coding: utf-8 -*-
import pytz

from odoo import models, fields, api

from ..models.mail_activity import MZ_DEFAULT_ACTIVITY_HOUR, mz_localize_to_utc


class MailActivitySchedule(models.TransientModel):
    _inherit = "mail.activity.schedule"

    mz_activity_time = fields.Float(
        string="Time",
        widget="float_time",
        help="Only used for Call / To-Do activities - Meeting activities get their real "
             "time from the Calendar event created on the next step instead. Kept as a "
             "plain backing field - the form shows mz_activity_datetime instead, a single "
             "combined picker, so the user never edits this one directly."
    )

    mz_activity_datetime = fields.Datetime(
        string="Scheduled For",
        compute="_compute_mz_activity_datetime",
        inverse="_inverse_mz_activity_datetime",
        help="Single date+time picker for Call/To-Do/Email activities, shown instead of "
             "separate Due Date and Time fields - mirrors how a Meeting is scheduled as one "
             "step. Not used for Meeting activities, whose time comes from the calendar "
             "event created on the next step instead."
    )

    @api.depends('date_deadline', 'mz_activity_time', 'activity_type_id')
    def _compute_mz_activity_datetime(self):
        for wiz in self:
            if wiz.activity_category == 'meeting' or not wiz.date_deadline:
                wiz.mz_activity_datetime = False
                continue
            hour = wiz.mz_activity_time or MZ_DEFAULT_ACTIVITY_HOUR
            tz_name = (wiz.activity_user_id.tz if wiz.activity_user_id else self.env.user.tz) or 'UTC'
            wiz.mz_activity_datetime = mz_localize_to_utc(wiz.date_deadline, hour, tz_name)

    def _inverse_mz_activity_datetime(self):
        for wiz in self:
            if not wiz.mz_activity_datetime:
                continue
            tz_name = (wiz.activity_user_id.tz if wiz.activity_user_id else self.env.user.tz) or 'UTC'
            localized = pytz.UTC.localize(wiz.mz_activity_datetime).astimezone(pytz.timezone(tz_name))
            wiz.date_deadline = localized.date()
            wiz.mz_activity_time = localized.hour + localized.minute / 60.0

    def _action_schedule_activities(self):
        activities = super(MailActivitySchedule, self)._action_schedule_activities()
        activities.mz_activity_time = self.mz_activity_time
        activities._mz_check_not_scheduled_in_past()
        return activities

    def _action_schedule_activities_personal(self):
        activity = super(MailActivitySchedule, self)._action_schedule_activities_personal()
        activity.mz_activity_time = self.mz_activity_time
        activity._mz_check_not_scheduled_in_past()
        return activity

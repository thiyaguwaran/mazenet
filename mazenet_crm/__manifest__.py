# -*- coding: utf-8 -*-
{
    'name': 'Mazenet CRM',
    'version': '19.0.1.0.0',
    'category': 'Sales/CRM',
    'summary': 'Mazenet CRM Pipeline Customizations',
    'description': """
Mazenet CRM - Pipeline Customizations
======================================
- 9 crm.team definitions & 8 pipeline stage sets
- RED-Lock overdue lead flag with kanban visual styling
- CTO mandatory-reason lead archive wizard
- Activity-driven Pipeline sort order, with same-day tie-breaking by time of day
- Meeting status tracking on calendar events
- 52 Placeholder Demo User Accounts
    """,
    'author': 'Mazenet Tech / Development Team',
    'website': 'https://www.mazenet.com',
    'depends': ['crm', 'mail', 'calendar', 'mazenet_access_rights'],
    "data": [
        "data/alarms.xml",
        "data/cron.xml",
        "data/stages.xml",
        "data/teams.xml",
        "data/utm_sources.xml",
        "security/ir.model.access.csv",
        "security/record_rules.xml",
        "views/calendar_event_views.xml",
        "views/crm_lead_views.xml",
        "views/crm_team_views.xml",
        "views/res_company_views.xml",
        "views/res_users_views.xml",
        "wizards/archive_wizard_views.xml",
        "wizards/mail_activity_schedule_views.xml",
        "wizards/mass_assign_wizard_views.xml"
    ],
    'assets': {
        'web.assets_backend': [
            'mazenet_crm/static/src/css/kanban_red.css',
        ],
    },
    'demo': [
        'demo/demo_data.xml',
    ],
    'license': 'AGPL-3',
    'installable': True,
    'application': True,
    'auto_install': False,
}

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
- Activity-driven Pipeline sort order
- Meeting status tracking on calendar events
- 52 Placeholder Demo User Accounts
    """,
    'author': 'Mazenet Tech / Development Team',
    'website': 'https://www.mazenet.com',
    'depends': ['crm', 'mail', 'calendar'],
    'data': [
        'security/ir.model.access.csv',
        'data/teams.xml',
        'data/stages.xml',
        'data/cron.xml',
        'data/alarms.xml',
        'views/crm_lead_views.xml',
        'views/crm_team_views.xml',
        'views/calendar_event_views.xml',
        'wizards/archive_wizard_views.xml',
        'demo/demo_data.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'mazenet_crm/static/src/css/kanban_red.css',
        ],
    },
    'demo': [],
    'license': 'AGPL-3',
    'installable': True,
    'application': True,
    'auto_install': False,
}

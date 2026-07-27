# -*- coding: utf-8 -*-
{
    'name': 'Mazenet CRM',
    'version': '19.0.1.0.0',
    'category': 'Sales/CRM',
    'summary': 'Mazenet CRM Hierarchy, Security & Access Control Framework',
    'description': """
Mazenet CRM - Milestone P1-M1 Foundation & Access Control
=========================================================
- 6 Security Groups under category Mazenet CRM
- Dynamic Supervision Hierarchy model (mz.supervision)
- Manager write content lock on non-owned leads
- 9 crm.team definitions & 8 pipeline stage sets
- CTO mandatory reason lead archive wizard
- 52 Placeholder Demo User Accounts & Reporting Lines
- RED-Lock Overdue Kanban Card Visual Styling
    """,
    'author': 'Mazenet Tech / Development Team',
    'website': 'https://www.mazenet.com',
    'depends': ['crm', 'mail', 'calendar'],
    'data': [
        'security/groups.xml',
        'security/ir.model.access.csv',
        'data/teams.xml',
        'data/stages.xml',
        'security/record_rules.xml',
        'data/cron.xml',
        'data/alarms.xml',
        'views/supervision_views.xml',
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

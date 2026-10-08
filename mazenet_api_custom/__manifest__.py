# -*- coding: utf-8 -*-
{
    'name': 'Mazenet API Customizations',
    'version': '19.0.1.0.1',
    'category': 'Sales/CRM',
    'summary': 'Mazenet API customizations for CRM',
    'description': """
        Mazenet API Customizations
        ==========================
        Custom API endpoints and CRM extensions for Mazenet.
    """,
    'author': 'Mazenet Tech / Development Team',
    'website': 'https://www.mazenet.com',
    'depends': ['base'],
    'data': [
        'security/ir.model.access.csv',
        'security/ir_rule.xml',
        'views/api_configuration_views.xml',
        'views/server_details_views.xml',
    ],
    'assets': {
        'web.assets_backend': [],
    },
    'demo': [],
    'license': 'AGPL-3',
    'installable': True,
    'application': False,
    'auto_install': False,
}
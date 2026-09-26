import logging

_logger = logging.getLogger(__name__)

# These three fields changed from Boolean to Datetime (client instruction, 2026-09-26).
# Postgres can't cast boolean -> timestamp directly, so Odoo's automatic schema sync
# fails with "cannot cast type boolean to timestamp without time zone". Convert them
# here first. Checked before writing this: all 599 existing crm_lead rows have all
# three columns False/unset, so there is no historical "True" value to preserve -
# every row becomes NULL (unknown datetime), with no data loss.
COLUMNS = [
    "x_feasibility_identified",
    "x_quote_shared_checkbox",
    "x_customer_goods_finalised",
]


def migrate(cr, version):
    _logger.info("========== PRE MIGRATION STARTED (mazenet_crm boolean->datetime) ==========")

    for column in COLUMNS:
        cr.execute(
            """
            SELECT data_type FROM information_schema.columns
            WHERE table_name = 'crm_lead' AND column_name = %s
            """,
            (column,),
        )
        row = cr.fetchone()
        if row and row[0] == "boolean":
            cr.execute(
                f'ALTER TABLE crm_lead ALTER COLUMN "{column}" TYPE timestamp USING NULL'
            )
            _logger.info("Converted crm_lead.%s from boolean to timestamp", column)

    _logger.info("========== PRE MIGRATION COMPLETED ==========")

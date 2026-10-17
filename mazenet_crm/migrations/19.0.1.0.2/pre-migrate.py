import logging

_logger = logging.getLogger(__name__)

BACKUP_TABLE = "mz_skill_type_team_backup"


def migrate(cr, version):
    """skill.type.team_id (Many2one) became team_ids (Many2many, skill_type_crm_team_rel).
    The old column is no longer a field, so the ORM would leave the mapping behind and
    the new relation would start empty - every lead would lose its Buisiness Unit. Park
    the old (skill_type, team) pairs in a side table here; post-migrate.py copies them
    into the new relation once the ORM has created it."""
    cr.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'skill_type' AND column_name = 'team_id'
        """
    )
    if not cr.fetchone():
        return
    cr.execute(f"DROP TABLE IF EXISTS {BACKUP_TABLE}")
    cr.execute(
        f"""
        CREATE TABLE {BACKUP_TABLE} AS
        SELECT id AS skill_type_id, team_id FROM skill_type WHERE team_id IS NOT NULL
        """
    )
    _logger.info("skill_type.team_id: backed up %s mapping(s) for team_ids migration", cr.rowcount)

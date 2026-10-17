import logging

_logger = logging.getLogger(__name__)

BACKUP_TABLE = "mz_skill_type_team_backup"


def migrate(cr, version):
    """Second half of pre-migrate.py: restore the old skill_type.team_id mapping into
    the new skill_type_crm_team_rel many2many. Idempotent, and skips a team or skill
    type that no longer exists."""
    cr.execute("SELECT to_regclass(%s)", (BACKUP_TABLE,))
    if not cr.fetchone()[0]:
        return
    cr.execute(
        f"""
        INSERT INTO skill_type_crm_team_rel (skill_type_id, team_id)
        SELECT b.skill_type_id, b.team_id FROM {BACKUP_TABLE} b
        JOIN skill_type s ON s.id = b.skill_type_id
        JOIN crm_team t ON t.id = b.team_id
        WHERE NOT EXISTS (
            SELECT 1 FROM skill_type_crm_team_rel r
            WHERE r.skill_type_id = b.skill_type_id AND r.team_id = b.team_id
        )
        """
    )
    _logger.info("skill_type_crm_team_rel: restored %s mapping(s) from skill_type.team_id", cr.rowcount)
    cr.execute(f"DROP TABLE {BACKUP_TABLE}")

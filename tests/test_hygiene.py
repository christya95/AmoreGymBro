import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_gitignore_excludes_secrets_and_databases():
    lines = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in lines
    assert "data/" in lines
    assert "*.db" in lines
    assert ".env.example" not in lines or "!.env.example" in lines
    example = (ROOT / ".env.example").read_text()
    assert "HEVY_API_KEY=" in example
    assert "\nHEVY_WEBHOOK_AUTHORIZATION=\n" in example
    assert "\nOPENAI_API_KEY=\n" in example
    assert "sk-" not in example
    result = subprocess.run(
        ["git", "check-ignore", "-v", ".env", "data/fitness.db", "coach.sqlite"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    ignored = result.stdout
    assert ".env" in ignored
    assert "data/fitness.db" in ignored
    assert "coach.sqlite" in ignored or "*.sqlite" in ignored


def test_compose_keeps_database_on_a_local_volume():
    text = (ROOT / "docker-compose.yml").read_text()
    assert "fitness_coach_data:/data" in text
    assert "smb://" not in text.lower()
    assert "/volumes/" not in text.lower()
    assert "127.0.0.1" in text


def test_mongodb_is_private_and_credentials_are_not_in_examples():
    import yaml
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    mongo = compose["services"]["mongodb"]
    assert "ports" not in mongo
    assert "fitness_coach_mongo:/data/db" in mongo["volumes"]
    assert compose["services"]["coach"]["environment"]["DATABASE_BACKEND"] == "mongodb"
    example = (ROOT / ".env.example").read_text()
    assert "\nMONGODB_PASSWORD=\n" in example
    assert "\nMONGO_INITDB_ROOT_PASSWORD=\n" in example
    local = yaml.safe_load((ROOT / "docker-compose.local.yml").read_text())
    assert local["services"]["mongodb"]["ports"] == ["127.0.0.1:27018:27017"]

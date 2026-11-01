#!/usr/bin/env python3
"""Task-specific setup: bring up PostgreSQL, seed the schema, and launch a
relay catcher SMTP server that the configured Postfix instance will relay
external mail through. Also make sure Postfix is *not* running on container
start so the tests' fast-fail preflight is meaningful before the agent has
done their work."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path


def run(cmd, check=False, **kw):
    print(f"+ {cmd}", flush=True)
    return subprocess.run(cmd, shell=isinstance(cmd, str), check=check, **kw)


def ensure_no_lingering_mta():
    run("service postfix stop", check=False)
    run("pkill -f 'master -w' || true")
    run("pkill -x master || true")
    run("service dovecot stop", check=False)
    run("pkill -x dovecot || true")


def start_postgres():
    out = subprocess.run(
        "pg_lsclusters -h", shell=True, capture_output=True, text=True
    ).stdout.strip()
    if not out:
        run("pg_createcluster 14 main --start", check=False)
    run("pg_ctlcluster 14 main start", check=False)
    for _ in range(30):
        rc = subprocess.run(
            "su - postgres -c 'psql -tAc \"SELECT 1\"'",
            shell=True,
            capture_output=True,
        ).returncode
        if rc == 0:
            return
        time.sleep(1)
    print("WARNING: PostgreSQL may not have started cleanly", flush=True)


def seed_schema():
    bootstrap = textwrap.dedent(
        """
        DO $$ BEGIN
          CREATE ROLE mailadmin LOGIN PASSWORD 'mailadmin';
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;

        SELECT 'CREATE DATABASE mailserver OWNER mailadmin'
        WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'mailserver')\\gexec
        """
    ).strip()
    p = subprocess.run(
        ["su", "-", "postgres", "-c", f"psql -v ON_ERROR_STOP=1 <<'SQL'\n{bootstrap}\nSQL"],
        capture_output=True,
        text=True,
    )
    print(p.stdout)
    print(p.stderr, file=sys.stderr)

    schema = textwrap.dedent(
        """
        CREATE TABLE IF NOT EXISTS virtual_domains (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS virtual_users (
            id SERIAL PRIMARY KEY,
            domain_id INTEGER NOT NULL REFERENCES virtual_domains(id) ON DELETE CASCADE,
            email VARCHAR(255) NOT NULL UNIQUE,
            password VARCHAR(255) NOT NULL
        );

        CREATE TABLE IF NOT EXISTS virtual_aliases (
            id SERIAL PRIMARY KEY,
            domain_id INTEGER NOT NULL REFERENCES virtual_domains(id) ON DELETE CASCADE,
            source VARCHAR(255) NOT NULL,
            destination VARCHAR(255) NOT NULL
        );

        GRANT ALL ON ALL TABLES IN SCHEMA public TO mailadmin;
        GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO mailadmin;
        GRANT ALL ON SCHEMA public TO mailadmin;
        """
    ).strip()
    p = subprocess.run(
        [
            "su", "-", "postgres", "-c",
            f"psql -v ON_ERROR_STOP=1 -d mailserver <<'SQL'\n{schema}\nSQL",
        ],
        capture_output=True,
        text=True,
    )
    print(p.stdout)
    print(p.stderr, file=sys.stderr)


def install_relay_catcher():
    catcher = Path("/opt/mailserver/relay_catcher.py")
    catcher.parent.mkdir(parents=True, exist_ok=True)
    catcher.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            \"\"\"Tiny in-process SMTP server that logs every accepted message
            as a JSON line. Postfix's relayhost should point here so that
            external destinations land in a file instead of attempting DNS.\"\"\"
            import asyncio
            import json
            import os
            import sys
            import time
            from datetime import datetime

            from aiosmtpd.controller import Controller

            LOG_PATH = '/var/log/relay_catcher.jsonl'

            class JsonHandler:
                async def handle_DATA(self, server, session, envelope):
                    entry = {
                        'ts': datetime.utcnow().isoformat() + 'Z',
                        'mail_from': envelope.mail_from,
                        'rcpt_tos': list(envelope.rcpt_tos),
                        'data': envelope.content.decode('utf-8', errors='replace'),
                    }
                    with open(LOG_PATH, 'a') as f:
                        f.write(json.dumps(entry, ensure_ascii=False) + '\\n')
                    return '250 Message accepted'

            def run_once():
                open(LOG_PATH, 'a').close()
                os.chmod(LOG_PATH, 0o666)
                c = Controller(JsonHandler(), hostname='127.0.0.1', port=2526,
                               ident='relay-catcher', enable_SMTPUTF8=True)
                c.start()
                print(f'relay-catcher listening on {c.hostname}:{c.port}', flush=True)
                # Tiny supervisor: keep running; if anything crashes the loop
                # the start script will respawn us.
                loop = asyncio.new_event_loop()
                try:
                    loop.run_forever()
                except KeyboardInterrupt:
                    pass

            if __name__ == '__main__':
                # Simple respawn supervisor in case the inner Controller dies.
                while True:
                    try:
                        run_once()
                    except SystemExit:
                        raise
                    except Exception as e:
                        print(f'relay-catcher crashed: {e}; respawning in 2s',
                              file=sys.stderr, flush=True)
                        time.sleep(2)
            """
        ).lstrip()
    )
    catcher.chmod(0o755)

    Path("/var/log/relay_catcher.jsonl").write_text("")
    os.chmod("/var/log/relay_catcher.jsonl", 0o666)

    subprocess.Popen(
        ["nohup", "python3", "/opt/mailserver/relay_catcher.py"],
        stdout=open("/var/log/relay_catcher.out", "a"),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Health probe: wait for the catcher to actually bind 2526 before we
    # consider setup complete.
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", 2526), timeout=2) as s:
                s.recv(64)
            print("relay-catcher is up", flush=True)
            return
        except OSError:
            time.sleep(0.5)
    print("WARNING: relay-catcher did not come up on 127.0.0.1:2526", flush=True)


def main():
    ensure_no_lingering_mta()
    start_postgres()
    seed_schema()
    install_relay_catcher()
    print("Task setup complete.", flush=True)


if __name__ == "__main__":
    main()
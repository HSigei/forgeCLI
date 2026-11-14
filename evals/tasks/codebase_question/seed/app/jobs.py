from .tickets import close_ticket, reopen_ticket


def nightly_job(tickets):
    for t in tickets:
        if t.get("stale"):
            close_ticket(t)


def morning_job(tickets):
    for t in tickets:
        if t.get("reopen"):
            reopen_ticket(t)

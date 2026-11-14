from .mail import render, send_email
from .audit import log_event


def notify_owner(ticket):
    send_email(ticket["owner"], render("Ticket {id} changed", id=ticket["id"]))


def close_ticket(ticket):
    ticket["open"] = False
    log_event("close", ticket)
    notify_owner(ticket)


def reopen_ticket(ticket):
    ticket["open"] = True
    log_event("reopen", ticket)


def escalate(ticket):
    ticket["priority"] = "high"
    notify_owner(ticket)

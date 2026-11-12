def render(template, **kw):
    return template.format(**kw)


def send_email(to, body):
    print("mail", to, body)

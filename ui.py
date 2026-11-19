"""Lancement direct depuis les anciennes configurations d’exécution."""

if __package__:
    from .multitools.ui import run
else:
    from multitools.ui import run


if __name__ == "__main__":
    run()

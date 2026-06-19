"""Core domain — application infrastructure shared by every other domain.

Connection/auth and the IG API client, the rate-limited API queue and guards,
the job scheduler, configuration, technical indicators and structured logging.
Holds no open/close decision logic — it is the plumbing the other domains run on.
"""

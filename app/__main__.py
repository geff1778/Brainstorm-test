"""BrainStorm — realtime AI quiz platform.

Package layout
--------------
``app.config``      typed settings loaded from the environment
``app.security``    password hashing, rate limiting, escaping helpers
``app.db``          SQLite connection helpers for both databases
``app.domain``      pure game models (rooms, teams, scoring) with no I/O
``app.services``    application logic (AI, accounts, leaderboard, UGC, ...)
``app.realtime``    Socket.IO event handlers, chat store, background tasks
``app.http_api``    Flask blueprints for the REST surface
"""

__version__ = "2.0.0"

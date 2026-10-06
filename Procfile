# Render / Heroku process definition.
#
# A single eventlet worker is required: room state lives in memory and Socket.IO
# sessions are pinned to the worker that accepted them.  See gunicorn.conf.py.
web: gunicorn -c gunicorn.conf.py wsgi:app

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("cyberlab_portal")

# Broker + result backend are redis on the internal compose network.
app.conf.broker_url = os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0")
app.conf.result_backend = os.environ.get("CELERY_RESULT_BACKEND", "redis://redis:6379/0")

# Read any CELERY_* settings from Django config, then autodiscover tasks.
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
# Also discover reaper.py modules (the orphan reaper lives outside tasks.py).
app.autodiscover_tasks(related_name="reaper")


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f"Request: {self.request!r}")

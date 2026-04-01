FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg

RUN pip install --no-cache-dir \
    Pillow \
    matplotlib \
    networkx \
    numpy \
    orjson \
    plotly \
    python-dateutil \
    pytz \
    pyyaml \
    regex \
    simplejson \
    sympy \
    tabulate

RUN mkdir -p /opt/matplotlib-seed \
    && MPLCONFIGDIR=/opt/matplotlib-seed python - <<'PY'
import matplotlib.pyplot as plt

plt.figure()
plt.plot([0, 1], [0, 1])
plt.savefig('/tmp/pytegbot-matplotlib-warmup.png')
plt.close('all')
PY

RUN rm -f /tmp/pytegbot-matplotlib-warmup.png

RUN useradd --create-home --uid 10001 runner
RUN chown -R runner:runner /opt/matplotlib-seed

WORKDIR /opt/runner
COPY runner/executor.py /opt/runner/executor.py

USER runner
ENTRYPOINT ["python", "/opt/runner/executor.py"]

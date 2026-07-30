FROM python:3.12-slim
WORKDIR /app
COPY ensemble-1d/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY base/ ./base/
COPY cross_alpha/ ./cross_alpha/
COPY ensemble-1d/app/ ./app/
COPY ensemble-1d/main.py ensemble-1d/spec.json ensemble-1d/config.toml ./
COPY ensemble-1d/data/ ./data/
COPY ensemble-1d/blacklist.txt .
COPY ensemble-1d/whitelist.txt .
# NOTE (see README.md "Known gap"): CrossSectionalEngine resolves each member
# alpha_id in spec.json's "members" list to <alphas_root>/<member_id>/spec.json,
# where <alphas_root> is SPEC_FILE's grandparent directory. Inside this
# container that's "/", so the 4 member spec.json files need to land at
# /1d-trend60cmf/spec.json etc. before this alpha can actually run
# containerized -- not yet wired here since this alpha isn't deployed
# (build/test only, see README.md).
CMD ["python", "main.py"]

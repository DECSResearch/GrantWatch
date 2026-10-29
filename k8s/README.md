# k8s manifests

Runs GrantWatch on the lab's k3s cluster in its own `grantwatch` namespace, the
same way `c2sr-testbed-llm-orchestrator` is deployed: the image is built on the
host and imported into k3s containerd (no registry), and secrets are rendered
from ansible-vault at deploy time. The cluster is on the lab network, so the
daily job can reach the Open WebUI model server.

| File | What |
|---|---|
| `namespace.yaml` | the `grantwatch` namespace |
| `configmap.yaml` | non-secret settings (model name, thinking off, batch sizes) injected via `envFrom` |
| `secret.example.yaml` | **example** `grantwatch-cred` and `grantwatch-profile`; real values come from ansible-vault |
| `postgres.yaml` | Postgres 16 with a 5 Gi volume, at `grantwatch-db:5432` |
| `web.yaml` | the dashboard, on NodePort `30810` |
| `cronjob-daily.yaml` | `python main.py` at 07:30 Central: download, score, load, email |

## Deploy (on the lambda box)

```bash
cp config/research_profile.yml config/research_profile.local.yml   # fill in summary and keywords
cp ansible/secrets.example.yml ansible/secrets.yml                  # fill in, then:
ansible-vault encrypt ansible/secrets.yml
ansible-playbook ansible/build.yml
ansible-playbook ansible/deploy.yml --ask-vault-pass
```

The deploy role needs the `kubernetes.core` collection
(`ansible-galaxy collection install kubernetes.core`) and the Python
`kubernetes` package on the host. Re-run both playbooks to roll out a change.

Run the daily job once right away instead of waiting for 07:30, then open
`http://<node-ip>:30810`:

```bash
k3s kubectl -n grantwatch create job --from=cronjob/grantwatch-daily first-run
k3s kubectl -n grantwatch logs -f job/first-run
```

The first run scores every open keyword match (about 15 minutes with Qwen
3.6, thinking off); later runs score only new grants. Without
`notify_recipients` in the secrets nothing is emailed, and the dashboard still
shows the recommendations.

For a quick try without Ansible, apply the files with `kubectl apply -f` in
the order above, after editing `secret.example.yaml`.

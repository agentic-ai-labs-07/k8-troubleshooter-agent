#!/usr/bin/env sh
# Writes two kubeconfigs for the kind cluster:
#   .kube/admin   kind's admin login. For you and for applying scenarios. Never mounted into the agent.
#   .kube/config  the agent's login: the k8s-troubleshooter ServiceAccount and a token that expires.
# Run again when the token expires (default 1h). Needs kind and kubectl.
set -eu

cd "$(dirname "$0")/.."
name=k8s-troubleshooter
duration="${TOKEN_DURATION:-1h}"
umask 077
mkdir -p .kube traces

kind get kubeconfig --name "$name" > .kube/admin
kubectl --kubeconfig .kube/admin apply -f rbac.yaml > /dev/null

server=$(kubectl --kubeconfig .kube/admin config view --minify -o jsonpath='{.clusters[0].cluster.server}')
ca=$(kubectl --kubeconfig .kube/admin config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
token=$(kubectl --kubeconfig .kube/admin create token "$name" -n "$name" --duration="$duration")

cat > .kube/config <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: $name
    cluster:
      server: $server
      certificate-authority-data: $ca
users:
  - name: $name
    user:
      token: $token
contexts:
  - name: $name
    context:
      cluster: $name
      user: $name
current-context: $name
EOF
chmod 600 .kube/admin .kube/config

echo "Wrote .kube/admin and .kube/config. The agent token expires in $duration."

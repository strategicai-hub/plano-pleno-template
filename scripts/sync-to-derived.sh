#!/usr/bin/env bash
# Aplica um commit deste template em todos os projetos derivados via cherry-pick.
#
# Uso:
#   ./scripts/sync-to-derived.sh <commit-sha>
#
# Fonte de verdade da lista de projetos derivados: CLAUDE.md
# (qualquer URL github.com listada fora do próprio template).

set -eo pipefail

COMMIT_SHA="${1:?Uso: $0 <commit-sha>}"
TEMPLATE_REPO="https://github.com/strategicai-hub/plano-pleno-template.git"
CLAUDE_MD="$(dirname "$0")/../CLAUDE.md"

if [ ! -f "$CLAUDE_MD" ]; then
    echo "[ERRO] CLAUDE.md não encontrado em $CLAUDE_MD" >&2
    exit 1
fi

# Extrai URLs de repos github.com do CLAUDE.md, exceto templates (start/pleno)
DERIVED_REPOS=$(grep -oE 'https://github\.com/[a-zA-Z0-9_-]+/[a-zA-Z0-9._-]+' "$CLAUDE_MD" \
    | grep -vE 'plano-(start|pleno)-template' \
    | sort -u)

if [ -z "$DERIVED_REPOS" ]; then
    echo "[AVISO] Nenhum projeto derivado encontrado em CLAUDE.md"
    exit 0
fi

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

echo "Sincronizando commit $COMMIT_SHA para os projetos:"
echo "$DERIVED_REPOS" | sed 's/^/  - /'
echo

SUCESSOS=()
FALHAS=()

for repo_url in $DERIVED_REPOS; do
    repo_name=$(basename "$repo_url" .git)
    echo "=== $repo_name ==="
    (
        cd "$TMPDIR"
        git clone --quiet "$repo_url" "$repo_name"
        cd "$repo_name"
        git remote add template "$TEMPLATE_REPO"
        git fetch --quiet template

        # Verifica se o commit já está aplicado
        if git log --oneline | grep -q "$(git log -1 --format=%s "$COMMIT_SHA" 2>/dev/null || echo '___nope___')" 2>/dev/null; then
            echo "[SKIP] Commit já aparece no histórico de $repo_name"
            exit 0
        fi

        if git cherry-pick -x "$COMMIT_SHA"; then
            git push origin HEAD
            echo "[OK] $repo_name atualizado"
        else
            echo "[ERRO] Cherry-pick conflitou em $repo_name — resolva manualmente"
            git cherry-pick --abort || true
            exit 1
        fi
    ) && SUCESSOS+=("$repo_name") || FALHAS+=("$repo_name")
    echo
done

echo "================================================"
echo "Sync concluído"
echo "  Sucessos: ${#SUCESSOS[@]} (${SUCESSOS[*]:-nenhum})"
echo "  Falhas:   ${#FALHAS[@]} (${FALHAS[*]:-nenhuma})"
echo "================================================"

[ ${#FALHAS[@]} -eq 0 ]

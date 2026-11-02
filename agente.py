#!/usr/bin/env python3
"""Agente Kavroo — análise semanal do catálogo da loja.

Fluxo:
1. Busca os produtos reais na Shopify Admin API (usando o token do secret).
2. Monta um resumo compacto de cada produto.
3. Envia esse resumo para o Claude analisar e gerar um relatório acionável.
4. Imprime o relatório no log do GitHub Actions.

Nenhuma dependência externa — só biblioteca padrão do Python.
"""
import json
import os
import sys
import urllib.error
import urllib.request

# ---- Configuração ---------------------------------------------------------
SHOP = "s2tecy-9k.myshopify.com"
API_VERSION = "2024-01"
# Modelo usado na análise. Troque aqui se quiser outro (ex.: claude-opus-5-5).
MODEL = "claude-sonnet-5-5"

STATIC_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN")      # modelo antigo (opcional)
CLIENT_ID = os.environ.get("SHOPIFY_CLIENT_ID")            # Dev Dashboard
CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET")   # Dev Dashboard
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")

if not ANTHROPIC_KEY:
    print("ERRO: falta o secret ANTHROPIC_API_KEY.")
    sys.exit(1)
if not STATIC_TOKEN and not (CLIENT_ID and CLIENT_SECRET):
    print("ERRO: configure SHOPIFY_ACCESS_TOKEN, ou SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET.")
    sys.exit(1)


def get_shop_token():
    """Usa o token estatico se existir; senao troca client_id/secret por um
    token de acesso via grant client_credentials (novo Dev Dashboard)."""
    if STATIC_TOKEN:
        return STATIC_TOKEN
    url = f"https://{SHOP}/admin/oauth/access_token"
    data = json.dumps({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials",
    }).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())["access_token"]


try:
    SHOP_TOKEN = get_shop_token()
except urllib.error.HTTPError as e:
    print("ERRO ao obter token da Shopify:", e.code, e.read().decode()[:1000])
    sys.exit(1)
except Exception as e:  # noqa: BLE001
    print("ERRO ao obter token da Shopify:", repr(e))
    sys.exit(1)


# ---- Shopify --------------------------------------------------------------
def shopify_get(path):
    url = f"https://{SHOP}/admin/api/{API_VERSION}/{path}"
    req = urllib.request.Request(
        url,
        headers={
            "X-Shopify-Access-Token": SHOP_TOKEN,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


try:
    data = shopify_get("products.json?limit=250")
except urllib.error.HTTPError as e:
    print("ERRO ao buscar produtos na Shopify:", e.code, e.read().decode()[:1000])
    sys.exit(1)
except Exception as e:  # noqa: BLE001
    print("ERRO ao buscar produtos na Shopify:", repr(e))
    sys.exit(1)

products = data.get("products", [])

# ---- Resumo compacto por produto -----------------------------------------
resumo = []
for p in products:
    variants = p.get("variants", []) or []
    prices = [v.get("price") for v in variants if v.get("price")]
    compare = [v.get("compare_at_price") for v in variants if v.get("compare_at_price")]
    inventory = sum((v.get("inventory_quantity") or 0) for v in variants)
    body = (p.get("body_html") or "").strip()
    resumo.append({
        "titulo": p.get("title"),
        "status": p.get("status"),
        "tipo": p.get("product_type") or None,
        "preco": prices[0] if prices else None,
        "preco_comparacao": compare[0] if compare else None,
        "variantes": len(variants),
        "imagens": len(p.get("images", []) or []),
        "tem_descricao": len(body) > 60,
        "tamanho_descricao": len(body),
        "estoque_total": inventory,
        "tags": p.get("tags") or "",
    })

ativos = sum(1 for r in resumo if r["status"] == "active")
rascunho = sum(1 for r in resumo if r["status"] == "draft")
arquivados = sum(1 for r in resumo if r["status"] == "archived")

# ---- Prompt ---------------------------------------------------------------
prompt = f"""Você é o gerente de e-commerce da loja Kavroo (kavroo.com), uma loja de \
dropshipping de produtos para pets (cães e gatos) nos EUA.

Estes são os dados REAIS dos {len(resumo)} produtos da loja, extraídos agora da Shopify \
({ativos} ativos, {rascunho} rascunho, {arquivados} arquivados):

{json.dumps(resumo, ensure_ascii=False, indent=1)}

Gere um RELATÓRIO SEMANAL objetivo e acionável, em português, com estas seções:

1. VISÃO GERAL — 2 a 3 linhas sobre a saúde do catálogo.
2. PROBLEMAS PRIORITÁRIOS — liste produtos com falhas, citando o título de cada um: \
sem imagem (imagens=0), sem descrição (tem_descricao=false) ou descrição curta \
(tamanho_descricao baixo), sem preço, ou com estoque_total=0.
3. OPORTUNIDADES DE RECEITA — ajustes de preço, bundles e cross-sell ENTRE os produtos \
desta lista, e quais produtos têm mais cara de "produto vencedor" para anúncio pago.
4. SEO & CONVERSÃO — 1 ou 2 títulos que dá para melhorar, com a versão reescrita.
5. TOP 3 AÇÕES DA SEMANA — as 3 tarefas mais importantes agora, em ordem de prioridade.

Regras: seja específico e direto, use os títulos reais dos produtos, NÃO invente dados \
que não estão na lista. Se algo precisar ser verificado manualmente, diga claramente."""

# ---- Anthropic ------------------------------------------------------------
req = urllib.request.Request(
    "https://api.anthropic.com/v1/messages",
    data=json.dumps({
        "model": MODEL,
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode(),
    headers={
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=180) as r:
        resp = json.loads(r.read().decode())
except urllib.error.HTTPError as e:
    print("ERRO na API da Anthropic:", e.code, e.read().decode()[:2000])
    sys.exit(1)
except Exception as e:  # noqa: BLE001
    print("ERRO na API da Anthropic:", repr(e))
    sys.exit(1)

print("=" * 64)
print(f"RELATÓRIO SEMANAL — AGENTE KAVROO  ({len(resumo)} produtos analisados)")
print("=" * 64)

printed = False
for block in resp.get("content", []):
    if block.get("type") == "text" and block.get("text", "").strip():
        print(block["text"])
        printed = True

if not printed:
    print("A API respondeu, mas sem texto utilizável. Diagnóstico:")
    diag = {k: resp.get(k) for k in ("model", "stop_reason", "stop_sequence", "usage", "type", "role")}
    print(json.dumps(diag, ensure_ascii=False, indent=2))
    print("content bruto:", json.dumps(resp.get("content"), ensure_ascii=False)[:1500])

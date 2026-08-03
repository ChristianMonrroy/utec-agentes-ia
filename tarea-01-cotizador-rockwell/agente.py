# -*- coding: utf-8 -*-
"""
Agente Cotizador de Tableros Eléctricos Rockwell
================================================
Tarea del programa de Agentes IA — UTEC Posgrado (MEng. Boris Alzamora)
Autor: Christian Monrroy — JYC Automática e Instrumentación S.A.C.

Agente ReAct (NO workflow) construido con `create_react_agent` de langgraph.prebuilt
sobre Ollama local (llama3.2).

Diseño:
  - AGÉNTICO: el LLM recibe un requerimiento de ingeniería en lenguaje natural,
    dimensiona el I/O, elige plataforma (CompactLogix vs ControlLogix), descubre
    obsolescencias y quiebres de stock, y replantea su propia configuración.
    El system prompt (prompt_sistema.md) fija OBJETIVO Y RESTRICCIONES, nunca
    una secuencia de pasos.
  - DETERMINISTA A PROPÓSITO: el cálculo de IGV (18%) y el formateo de la tabla
    final son código fijo (ver sección "CAPA DETERMINISTA"), fuera del control
    del LLM. Está declarado como workflow intencional.

Archivos:
    agente.py          catálogo, 8 herramientas, agente y capa determinista
    prompt_sistema.md  system prompt (objetivo + restricciones)
    cotizaciones/      una corrida por archivo markdown (traza + auditoría + tabla)

Uso:
    python agente.py
    python agente.py "12 motores 10HP, 8 señales 4-20mA, EtherNet/IP"
"""

from __future__ import annotations

import ast
import operator
import os
import re
import sys
import unicodedata
import warnings
from datetime import datetime
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_ollama import ChatOllama
from langgraph.errors import GraphRecursionError

# create_react_agent está marcado como deprecado en LangGraph 1.x a favor de
# langchain.agents.create_agent; se mantiene por requisito del enunciado.
warnings.filterwarnings("ignore", message=".*create_react_agent.*")

from langgraph.prebuilt import create_react_agent  # noqa: E402

# Modelo de Ollama. Se puede cambiar sin tocar el código:
#   $env:MODELO_OLLAMA = "qwen2.5:7b"; python agente.py
MODELO = os.environ.get("MODELO_OLLAMA", "llama3.2")
IGV = 0.18
MONEDA = "USD"
MAX_CICLOS_AUDITORIA = 2  # rebotes máximos del auditor antes de entregar lo que haya


# ============================================================================
# CATÁLOGO (datos de referencia — precios y consumos de orden realista)
# ============================================================================
# estado: "ACTIVO" | "DESCONTINUADO"
# slots:  posiciones de chasis que ocupa (fuentes y chasis no ocupan slot)
# ma_5v / ma_24v: consumo de backplane, en miliamperios

CATALOGO: dict[str, dict] = {
    # ---------------- Controladores ----------------
    "5069-L306ER": {
        "familia": "controlador",
        "plataforma": "CompactLogix 5380",
        "descripcion": "Controlador CompactLogix 5380, 3 MB memoria usuario, 2x puertos EtherNet/IP embebidos, hasta 8 módulos de I/O locales Compact 5000",
        "precio": 3150.00,
        "estado": "ACTIVO",
        "stock": 4,
        "lead_time_semanas": 6,
        "slots": 0,
        "ma_5v": 0,
        "ma_24v": 0,
        "max_modulos_locales": 8,
        "notas": "No requiere chasis ni fuente de chasis: alimentación 18-32 VDC directa. Ethernet embebido.",
    },
    "1756-L83E": {
        "familia": "controlador",
        "plataforma": "ControlLogix 5580",
        "descripcion": "Controlador ControlLogix 5580, 40 MB memoria usuario, puerto EtherNet/IP 1 Gb embebido",
        "precio": 14900.00,
        "estado": "ACTIVO",
        "stock": 2,
        "lead_time_semanas": 10,
        "slots": 1,
        "ma_5v": 1250,
        "ma_24v": 0,
        "notas": "Requiere chasis 1756 y fuente de alimentación 1756. Ocupa 1 slot.",
    },
    # ---------------- I/O ControlLogix (1756) ----------------
    "1756-IF8": {
        "familia": "entrada analogica",
        "plataforma": "ControlLogix",
        "descripcion": "Módulo de entrada analógica 8 canales, 4-20 mA / +-10 VDC, no aislado",
        "precio": 1980.00,
        "estado": "ACTIVO",
        "stock": 0,
        "lead_time_semanas": 22,
        "slots": 1,
        "ma_5v": 150,
        "ma_24v": 250,
        "canales": 8,
        "notas": "Sin existencias en almacén Lima. Backorder de fábrica.",
    },
    "1756-IF8I": {
        "familia": "entrada analogica",
        "plataforma": "ControlLogix",
        "descripcion": "Módulo de entrada analógica 8 canales aislados individualmente, 4-20 mA / +-10 VDC, HART opcional",
        "precio": 2650.00,
        "estado": "ACTIVO",
        "stock": 6,
        "lead_time_semanas": 4,
        "slots": 1,
        "ma_5v": 300,
        "ma_24v": 200,
        "canales": 8,
        "notas": "Aislamiento canal a canal. Reemplazo funcional del 1756-IF8.",
    },
    "1756-IB16": {
        "familia": "entrada digital",
        "plataforma": "ControlLogix",
        "descripcion": "Módulo de entrada digital 16 puntos, 10-31.2 VDC, sink",
        "precio": 590.00,
        "estado": "ACTIVO",
        "stock": 20,
        "lead_time_semanas": 3,
        "slots": 1,
        "ma_5v": 100,
        "ma_24v": 3,
        "canales": 16,
        "notas": "",
    },
    "1756-OB16E": {
        "familia": "salida digital",
        "plataforma": "ControlLogix",
        "descripcion": "Módulo de salida digital 16 puntos, 10-31.2 VDC, source, con fusible electrónico",
        "precio": 780.00,
        "estado": "ACTIVO",
        "stock": 12,
        "lead_time_semanas": 3,
        "slots": 1,
        "ma_5v": 250,
        "ma_24v": 2,
        "canales": 16,
        "notas": "También referido como 1756-OB16.",
    },
    # ---------------- I/O Compact 5000 (5069) ----------------
    "5069-IF8": {
        "familia": "entrada analogica",
        "plataforma": "CompactLogix",
        "descripcion": "Módulo Compact 5000 de entrada analógica 8 canales, 4-20 mA / +-10 VDC",
        "precio": 1240.00,
        "estado": "ACTIVO",
        "stock": 9,
        "lead_time_semanas": 5,
        "slots": 1,
        "ma_5v": 0,
        "ma_24v": 0,
        "canales": 8,
        "notas": "Cuenta como módulo local del CompactLogix, no ocupa slot de chasis 1756.",
    },
    "5069-IB16": {
        "familia": "entrada digital",
        "plataforma": "CompactLogix",
        "descripcion": "Módulo Compact 5000 de entrada digital 16 puntos, 24 VDC",
        "precio": 430.00,
        "estado": "ACTIVO",
        "stock": 18,
        "lead_time_semanas": 4,
        "slots": 1,
        "ma_5v": 0,
        "ma_24v": 0,
        "canales": 16,
        "notas": "Cuenta como módulo local del CompactLogix.",
    },
    "5069-OB16": {
        "familia": "salida digital",
        "plataforma": "CompactLogix",
        "descripcion": "Módulo Compact 5000 de salida digital 16 puntos, 24 VDC, source",
        "precio": 520.00,
        "estado": "ACTIVO",
        "stock": 15,
        "lead_time_semanas": 4,
        "slots": 1,
        "ma_5v": 0,
        "ma_24v": 0,
        "canales": 16,
        "notas": "Cuenta como módulo local del CompactLogix.",
    },
    # ---------------- Fuentes 1756 ----------------
    "1756-PA72": {
        "familia": "fuente",
        "plataforma": "ControlLogix",
        "descripcion": "Fuente de alimentación ControlLogix, entrada 85-265 VAC, salida 5.1 V @ 10 A / 24 V @ 2.8 A / 3.3 V @ 4 A",
        "precio": 1180.00,
        "estado": "DESCONTINUADO",
        "stock": 3,
        "lead_time_semanas": 0,
        "slots": 0,
        "ma_5v": 0,
        "ma_24v": 0,
        "cap_5v_ma": 10000,
        "cap_24v_ma": 2800,
        "notas": "Producto descontinuado por el fabricante. Sin soporte ni reposición. No cotizar en proyectos nuevos.",
    },
    "1756-PA75": {
        "familia": "fuente",
        "plataforma": "ControlLogix",
        "descripcion": "Fuente de alimentación ControlLogix, entrada 85-265 VAC, salida 5.1 V @ 13 A / 24 V @ 2.8 A / 3.3 V @ 4 A",
        "precio": 1450.00,
        "estado": "ACTIVO",
        "stock": 5,
        "lead_time_semanas": 6,
        "slots": 0,
        "ma_5v": 0,
        "ma_24v": 0,
        "cap_5v_ma": 13000,
        "cap_24v_ma": 2800,
        "notas": "Sucesora directa de la 1756-PA72. Alimentación en corriente alterna.",
    },
    "1756-PB75": {
        "familia": "fuente",
        "plataforma": "ControlLogix",
        "descripcion": "Fuente de alimentación ControlLogix, entrada 18-32 VDC, salida 5.1 V @ 13 A / 24 V @ 2.8 A / 3.3 V @ 4 A",
        "precio": 1520.00,
        "estado": "ACTIVO",
        "stock": 3,
        "lead_time_semanas": 7,
        "slots": 0,
        "ma_5v": 0,
        "ma_24v": 0,
        "cap_5v_ma": 13000,
        "cap_24v_ma": 2800,
        "notas": "Alimentación en corriente continua 24 VDC. Usar solo si el tablero tiene bus DC respaldado.",
    },
    # ---------------- Chasis 1756 ----------------
    "1756-A7": {
        "familia": "chasis",
        "plataforma": "ControlLogix",
        "descripcion": "Chasis ControlLogix de 7 slots",
        "precio": 690.00,
        "estado": "ACTIVO",
        "stock": 8,
        "lead_time_semanas": 5,
        "slots": 0,
        "ma_5v": 0,
        "ma_24v": 0,
        "capacidad_slots": 7,
        "notas": "La fuente 1756 se monta a la izquierda del chasis y no consume slot.",
    },
    "1756-A10": {
        "familia": "chasis",
        "plataforma": "ControlLogix",
        "descripcion": "Chasis ControlLogix de 10 slots",
        "precio": 890.00,
        "estado": "ACTIVO",
        "stock": 6,
        "lead_time_semanas": 5,
        "slots": 0,
        "ma_5v": 0,
        "ma_24v": 0,
        "capacidad_slots": 10,
        "notas": "La fuente 1756 se monta a la izquierda del chasis y no consume slot.",
    },
}

# Sucesores declarados por ingeniería de producto
REEMPLAZOS: dict[str, list[tuple[str, str]]] = {
    "1756-PA72": [
        ("1756-PA75", "Sucesora directa declarada por el fabricante; misma entrada AC, mayor capacidad en 5.1 V (13 A vs 10 A)."),
        ("1756-PB75", "Alternativa solo si el tablero alimenta el chasis desde un bus de 24 VDC."),
    ],
    "1756-IF8": [
        ("1756-IF8I", "Mismo conteo de canales 4-20 mA con aislamiento individual; disponible en stock local."),
    ],
}

# Historial comercial de JYC (referencial, para benchmark de precios y alcance)
HISTORICO = [
    {
        "proyecto": "SPCC Toquepala - Migración PLC5 a ControlLogix, planta de molienda",
        "anio": 2023,
        "cliente": "SPCC",
        "plataforma": "ControlLogix 1756-L83E",
        "io": "96 DI, 64 DO, 40 AI",
        "monto_usd": 128500,
        "nota": "Chasis 1756-A10 redundado, fuentes 1756-PA75. Margen de slots libres exigido por el cliente: 20%.",
    },
    {
        "proyecto": "Antapaccay - Tablero de control de espesadores",
        "anio": 2024,
        "cliente": "Antapaccay",
        "plataforma": "ControlLogix 1756-L83E",
        "io": "48 DI, 32 DO, 24 AI",
        "monto_usd": 74200,
        "nota": "Se usó 1756-IF8I por requerimiento de aislamiento canal a canal en lazos de densidad.",
    },
    {
        "proyecto": "Agrokasa - Automatización de fertirriego, fundo Santa Rita",
        "anio": 2024,
        "cliente": "Agrokasa",
        "plataforma": "CompactLogix 5069-L306ER",
        "io": "32 DI, 16 DO, 16 AI",
        "monto_usd": 31800,
        "nota": "CompactLogix elegido por conteo de I/O bajo y presupuesto acotado. 6 módulos Compact 5000 locales.",
    },
    {
        "proyecto": "Ambev Perú - Sala de máquinas, control de compresores",
        "anio": 2022,
        "cliente": "Ambev",
        "plataforma": "CompactLogix 5069-L306ER",
        "io": "24 DI, 16 DO, 12 AI",
        "monto_usd": 26400,
        "nota": "Se agotó la expansión local a 8 módulos; una ampliación posterior obligó a migrar a ControlLogix.",
    },
]


# ============================================================================
# UTILIDADES INTERNAS (no son herramientas del agente)
# ============================================================================

_PATRON_ITEM = re.compile(
    r"(?:(\d+)\s*[xX*]\s*)?([0-9]{3,4}-[A-Za-z0-9\-]+)(?:\s*[xX*:]?\s*\(?\s*(\d+)\s*\)?)?"
)


def _norm(codigo: str) -> str:
    return (codigo or "").strip().upper().replace(" ", "")


def _parsear_lista(texto: str) -> list[tuple[str, int]]:
    """Convierte '1756-L83E, 3x 1756-IF8, 1756-IB16 x2' en [(codigo, cantidad), ...]."""
    items: list[tuple[str, int]] = []
    for m in _PATRON_ITEM.finditer(texto or ""):
        antes, codigo, despues = m.group(1), _norm(m.group(2)), m.group(3)
        cantidad = int(antes or despues or 1)
        items.append((codigo, max(1, cantidad)))
    return items


def _buscar(codigo: str) -> dict | None:
    return CATALOGO.get(_norm(codigo))


def _sugerir(codigo: str, familia: str | None = None) -> str:
    """Propone códigos cercanos cuando el agente envía uno parcial o inexistente."""
    c = _norm(codigo)
    raiz = c.split("-")[0] if c else ""
    cand = [
        k
        for k, v in CATALOGO.items()
        if (familia is None or v["familia"] == familia)
        and (not c or c in k or (raiz and k.startswith(raiz)))
    ]
    if not cand:
        cand = [k for k, v in CATALOGO.items() if familia is None or v["familia"] == familia]
    return ", ".join(cand[:8])


# ============================================================================
# HERRAMIENTAS DEL AGENTE (8)
# ============================================================================


@tool
def buscar_catalogo(consulta: str) -> str:
    """Busca productos Rockwell en el catálogo de JYC por texto libre.

    Acepta términos como 'entrada analogica', 'salida digital', 'fuente',
    'chasis', 'controlador', 'CompactLogix', 'ControlLogix', '4-20 mA' o un
    código parcial como '1756'. Devuelve código, descripción, precio y estado
    comercial de cada coincidencia.
    """
    q = (consulta or "").strip().lower()
    if not q:
        return "Indica un término de búsqueda (ej.: 'entrada analogica', 'fuente', 'chasis', 'controlador')."

    palabras = [p for p in re.split(r"[\s,;]+", q) if len(p) > 2] or [q]
    puntuados = []
    for codigo, p in CATALOGO.items():
        blob = f"{codigo} {p['familia']} {p['plataforma']} {p['descripcion']}".lower()
        score = sum(1 for w in palabras if w in blob)
        if score:
            puntuados.append((score, codigo, p))

    # Solo las coincidencias más específicas, para no devolver medio catálogo
    mejor = max((s for s, _, _ in puntuados), default=0)
    resultados = [(c, p) for s, c, p in puntuados if s == mejor]

    if not resultados:
        return f"Sin coincidencias para '{consulta}'. Familias disponibles: controlador, entrada digital, salida digital, entrada analogica, fuente, chasis."

    lineas = [f"{len(resultados)} coincidencia(s) para '{consulta}':"]
    for codigo, p in resultados:
        marca = "" if p["estado"] == "ACTIVO" else "  [!] DESCONTINUADO"
        lineas.append(
            f"- {codigo} | {p['plataforma']} | {p['descripcion']} | {MONEDA} {p['precio']:,.2f}{marca}"
        )
    return "\n".join(lineas)


@tool
def detalle_producto(codigo: str) -> str:
    """Devuelve la ficha técnica de un producto: canales, consumo de backplane,
    slots que ocupa, capacidad (si es fuente o chasis), precio unitario, estado
    comercial y notas de aplicación. Recibe un código exacto, ej.: '1756-IF8'.
    """
    p = _buscar(codigo)
    if not p:
        return (
            f"El código '{codigo}' no existe en el catálogo. Debe ser un código completo. "
            f"Códigos parecidos: {_sugerir(codigo)}."
        )

    c = _norm(codigo)
    l = [
        f"Ficha técnica {c}",
        f"  Familia: {p['familia']} | Plataforma: {p['plataforma']}",
        f"  Descripción: {p['descripcion']}",
        f"  Precio unitario: {MONEDA} {p['precio']:,.2f} (sin IGV)",
        f"  Estado comercial: {p['estado']}",
    ]
    if "canales" in p:
        l.append(f"  Canales/puntos por módulo: {p['canales']}")
    if p.get("slots"):
        l.append(f"  Slots de chasis que ocupa: {p['slots']}")
    else:
        l.append("  Slots de chasis que ocupa: 0")
    if p["familia"] not in ("fuente", "chasis"):
        l.append(f"  Consumo de backplane: {p['ma_5v']} mA @ 5.1 V | {p['ma_24v']} mA @ 24 V")
    if "cap_5v_ma" in p:
        l.append(f"  Capacidad de salida: {p['cap_5v_ma']} mA @ 5.1 V | {p['cap_24v_ma']} mA @ 24 V")
    if "capacidad_slots" in p:
        l.append(f"  Capacidad del chasis: {p['capacidad_slots']} slots")
    if "max_modulos_locales" in p:
        l.append(f"  Máximo de módulos de I/O locales soportados: {p['max_modulos_locales']}")
    if p["notas"]:
        l.append(f"  Notas: {p['notas']}")
    return "\n".join(l)


@tool
def verificar_stock(codigo: str, cantidad: int) -> str:
    """Verifica disponibilidad real y vigencia comercial de un producto para la
    cantidad que se piensa cotizar. Informa stock en almacén, faltante, lead time
    y si el producto está descontinuado. Ejecutar antes de dar por cerrada
    cualquier línea de la cotización.
    """
    p = _buscar(codigo)
    if not p:
        return (
            f"El código '{codigo}' no existe en el catálogo, no se puede verificar stock. "
            f"Códigos parecidos: {_sugerir(codigo)}."
        )

    c = _norm(codigo)
    try:
        cant = max(1, int(cantidad))
    except (TypeError, ValueError):
        cant = 1
    stock = p["stock"]

    if p["estado"] == "DESCONTINUADO":
        return (
            f"[BLOQUEANTE] {c} está DESCONTINUADO por el fabricante. "
            f"Existencias residuales: {stock} un., sin reposición ni soporte. "
            f"No es cotizable en un proyecto nuevo: hay que sustituirlo."
        )
    if stock == 0:
        return (
            f"[BLOQUEANTE] {c}: stock 0 en almacén Lima. Requerido: {cant} un. "
            f"Lead time de fábrica: {p['lead_time_semanas']} semanas, inaceptable para el proyecto. "
            f"Hay que sustituirlo por una alternativa disponible."
        )
    if stock < cant:
        return (
            f"[PARCIAL] {c}: stock {stock} un., requerido {cant} un. Faltan {cant - stock} un. "
            f"con lead time de {p['lead_time_semanas']} semanas. Evaluar sustitución o entrega parcial."
        )
    return (
        f"[OK] {c}: stock {stock} un. disponible, cubre las {cant} un. requeridas. "
        f"Producto {p['estado']}. Precio unitario {MONEDA} {p['precio']:,.2f}."
    )


@tool
def buscar_reemplazo(codigo: str) -> str:
    """Devuelve los sucesores o alternativas técnicas válidas para un producto
    descontinuado o sin stock, con su justificación, precio y disponibilidad.
    Si no hay sucesor declarado, propone equivalentes de la misma familia.
    """
    c = _norm(codigo)
    p = _buscar(c)
    if not p:
        return f"El código '{codigo}' no existe en el catálogo. Códigos parecidos: {_sugerir(codigo)}."

    candidatos = REEMPLAZOS.get(c, [])
    if not candidatos:
        candidatos = [
            (k, f"Misma familia ({v['familia']}) y plataforma {v['plataforma']}.")
            for k, v in CATALOGO.items()
            if k != c and v["familia"] == p["familia"] and v["plataforma"] == p["plataforma"] and v["estado"] == "ACTIVO"
        ]
    if not candidatos:
        return f"No hay reemplazo registrado para {c}. Replantea la arquitectura con otra familia o plataforma."

    l = [f"Alternativas para {c} (delta de precio contra {MONEDA} {p['precio']:,.2f}):"]
    for alt, motivo in candidatos:
        a = CATALOGO.get(alt)
        if not a:
            continue
        delta = a["precio"] - p["precio"]
        disp = f"stock {a['stock']} un." if a["stock"] > 0 else "SIN STOCK"
        l.append(
            f"- {alt} | {a['descripcion']} | {MONEDA} {a['precio']:,.2f} "
            f"({'+' if delta >= 0 else ''}{delta:,.2f}) | {a['estado']} | {disp}\n"
            f"    Motivo: {motivo}"
        )
    l.append("El reemplazo cambia consumo y precio: revalida la configuración afectada.")
    return "\n".join(l)


@tool
def dimensionar_fuente(codigo_fuente: str, modulos: str) -> str:
    """Verifica si una fuente 1756 soporta el consumo de backplane de un conjunto
    de módulos. 'modulos' es una lista en texto, ej.: '1756-L83E, 3x 1756-IF8,
    2x 1756-IB16, 1756-OB16E'. Devuelve consumo total por riel, porcentaje de
    utilización y si la fuente es SUFICIENTE o EXCEDIDA. Una fuente excedida
    invalida la configuración: hay que cambiar de fuente o partir en dos racks.
    """
    f = _buscar(codigo_fuente)
    if not f or "cap_5v_ma" not in f:
        return (
            f"'{codigo_fuente}' no es una fuente válida. Debes indicar el código de una fuente: "
            f"{_sugerir('', 'fuente')}."
        )

    items = _parsear_lista(modulos)
    if not items:
        return "No se reconocieron módulos. Formato esperado: '1756-L83E, 3x 1756-IF8, 2x 1756-IB16'."

    tot5 = tot24 = 0
    detalle, ignorados = [], []
    for codigo, cant in items:
        p = _buscar(codigo)
        if not p:
            ignorados.append(f"{codigo} (no existe en catálogo)")
            continue
        if p["familia"] in ("fuente", "chasis"):
            ignorados.append(f"{codigo} (no consume backplane)")
            continue
        c5, c24 = p["ma_5v"] * cant, p["ma_24v"] * cant
        tot5 += c5
        tot24 += c24
        detalle.append(f"  {cant} x {codigo}: {c5} mA @5.1V, {c24} mA @24V")

    cap5, cap24 = f["cap_5v_ma"], f["cap_24v_ma"]
    u5 = tot5 / cap5 * 100 if cap5 else 0
    u24 = tot24 / cap24 * 100 if cap24 else 0

    l = [f"Dimensionamiento con {_norm(codigo_fuente)} (capacidad {cap5} mA @5.1V / {cap24} mA @24V):"]
    l += detalle
    if ignorados:
        l.append("  Ignorados: " + ", ".join(ignorados))
    l.append(f"  TOTAL: {tot5} mA @5.1V ({u5:.0f}% de uso) | {tot24} mA @24V ({u24:.0f}% de uso)")

    if tot5 > cap5 or tot24 > cap24:
        l.append("  RESULTADO: EXCEDIDA. La fuente no soporta esta configuración. Configuración inválida.")
    elif u5 > 80 or u24 > 80:
        l.append("  RESULTADO: SUFICIENTE PERO AL LÍMITE (>80%). Sin margen para ampliaciones futuras.")
    else:
        l.append("  RESULTADO: SUFICIENTE, con margen de reserva.")
    if f["estado"] == "DESCONTINUADO":
        l.append(f"  ADVERTENCIA: {_norm(codigo_fuente)} está DESCONTINUADA, el cálculo no la habilita para cotizar.")
    return "\n".join(l)


@tool
def verificar_slots_chasis(codigo_chasis: str, modulos: str) -> str:
    """Verifica si un chasis 1756 (o el backplane local de un CompactLogix)
    aloja el conjunto de módulos indicado. 'modulos' es una lista en texto, ej.:
    '1756-L83E, 3x 1756-IF8, 2x 1756-IB16'. Devuelve slots requeridos, capacidad,
    libres y si la configuración CABE, va JUSTA o EXCEDE. Si excede, la
    configuración es inválida y hay que reconsiderar chasis o plataforma.
    """
    ch = _buscar(codigo_chasis)
    if not ch:
        return (
            f"'{codigo_chasis}' no existe en el catálogo. Chasis disponibles: 1756-A7 (7 slots), "
            f"1756-A10 (10 slots). Para CompactLogix indica el código del controlador (5069-L306ER)."
        )

    if "capacidad_slots" in ch:
        cap, etiqueta = ch["capacidad_slots"], "slots de chasis"
    elif "max_modulos_locales" in ch:
        cap, etiqueta = ch["max_modulos_locales"], "módulos de I/O locales"
    else:
        return f"'{codigo_chasis}' no es un chasis ni un controlador con expansión local."

    items = _parsear_lista(modulos)
    if not items:
        return "No se reconocieron módulos. Formato esperado: '1756-L83E, 3x 1756-IF8, 2x 1756-IB16'."

    usados = 0
    detalle, ignorados = [], []
    for codigo, cant in items:
        p = _buscar(codigo)
        if not p:
            ignorados.append(f"{codigo} (no existe)")
            continue
        if _norm(codigo) == _norm(codigo_chasis):
            continue  # el propio chasis/controlador no se cuenta contra su capacidad
        if p["familia"] in ("fuente", "chasis"):
            ignorados.append(f"{codigo} (no ocupa slot)")
            continue
        usados += p["slots"] * cant
        detalle.append(f"  {cant} x {codigo}: {p['slots'] * cant} {etiqueta}")

    libres = cap - usados
    l = [f"Ocupación de {_norm(codigo_chasis)} (capacidad {cap} {etiqueta}):"]
    l += detalle
    if ignorados:
        l.append("  No computados: " + ", ".join(ignorados))
    l.append(f"  TOTAL OCUPADO: {usados} de {cap} | LIBRES: {libres}")

    if libres < 0:
        l.append(f"  RESULTADO: EXCEDE por {abs(libres)}. Configuración inválida: se requiere mayor capacidad o cambio de plataforma.")
    elif libres == 0:
        l.append("  RESULTADO: JUSTO, 0 libres. No queda reserva para crecimiento.")
    else:
        l.append(f"  RESULTADO: CABE, con {libres} de reserva.")
    return "\n".join(l)


_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval_nodo(n):
    if isinstance(n, ast.Expression):
        return _eval_nodo(n.body)
    if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
        return n.value
    if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
        return _OPS[type(n.op)](_eval_nodo(n.left), _eval_nodo(n.right))
    if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
        return _OPS[type(n.op)](_eval_nodo(n.operand))
    raise ValueError("expresión no permitida")


@tool
def calcular(expresion: str) -> str:
    """Evalúa una expresión aritmética. Úsala para todo cálculo numérico
    (conteo de puntos de I/O, cantidad de módulos, sumas de precios, consumos)
    en lugar de calcular mentalmente. Ej.: '(14*2+6)/16', '3*1980+890'.
    """
    try:
        r = _eval_nodo(ast.parse((expresion or "").strip(), mode="eval"))
    except Exception:
        return f"Expresión inválida: '{expresion}'. Usa solo números y + - * / // % ** y paréntesis."
    if isinstance(r, float) and r.is_integer():
        r = int(r)
    txt = f"{r:,.4f}".rstrip("0").rstrip(".") if isinstance(r, float) else f"{r:,}"
    return f"{expresion} = {txt}"


@tool
def consultar_historico(termino: str) -> str:
    """Consulta proyectos ejecutados por JYC para contrastar plataforma, alcance
    de I/O, precios y lecciones aprendidas. Busca por cliente (SPCC, Antapaccay,
    Agrokasa, Ambev), plataforma (CompactLogix, ControlLogix) o palabra clave.
    Usa 'todos' para listar el historial completo.
    """
    q = (termino or "").strip().lower()
    if q in ("", "todos", "todo"):
        sel = HISTORICO
    else:
        sel = [h for h in HISTORICO if q in " ".join(str(v) for v in h.values()).lower()]
    if not sel:
        return f"Sin proyectos que coincidan con '{termino}'. Clientes registrados: SPCC, Antapaccay, Agrokasa, Ambev."

    l = [f"{len(sel)} proyecto(s) en el histórico de JYC:"]
    for h in sel:
        l.append(
            f"- [{h['anio']}] {h['proyecto']}\n"
            f"    Plataforma: {h['plataforma']} | I/O: {h['io']} | Monto: {MONEDA} {h['monto_usd']:,.0f}\n"
            f"    Lección: {h['nota']}"
        )
    return "\n".join(l)


HERRAMIENTAS = [
    buscar_catalogo,
    detalle_producto,
    verificar_stock,
    buscar_reemplazo,
    dimensionar_fuente,
    verificar_slots_chasis,
    calcular,
    consultar_historico,
]


# ============================================================================
# SYSTEM PROMPT — OBJETIVO + RESTRICCIONES (sin secuencia de pasos)
# ----------------------------------------------------------------------------
# Vive en prompt_sistema.md, no en el código: es el artefacto de diseño que se
# discute en la entrega y se puede iterar sin tocar Python.
# ============================================================================

RUTA_PROMPT = Path(__file__).resolve().parent / "prompt_sistema.md"


def cargar_system_prompt(ruta: Path = RUTA_PROMPT) -> str:
    """Lee el system prompt desde el archivo markdown.

    Se relee en cada ejecución para poder ajustar las restricciones sin
    modificar el código. Falla de forma explícita si el archivo no está: correr
    el agente sin sus restricciones daría resultados silenciosamente inválidos.
    """
    try:
        contenido = ruta.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise SystemExit(f"[ERROR] No se pudo leer el system prompt en {ruta}: {e}")
    if not contenido:
        raise SystemExit(f"[ERROR] El system prompt en {ruta} está vacío.")
    return contenido


# ============================================================================
# CAPA DETERMINISTA (workflow a propósito — declarado en el documento)
# ----------------------------------------------------------------------------
# El LLM no calcula IGV ni arma la tabla final. Estas rutas de código son fijas:
# extraen las líneas efectivamente validadas durante la traza del agente, aplican
# el IGV de 18% y formatean la cotización. Es la parte NO agéntica del sistema.
# ============================================================================


def extraer_items_de_traza(mensajes) -> list[tuple[str, int]]:
    """Recorre la traza del agente y reconstruye la lista de materiales.

    Regla determinista: cada verificación de stock es una intención de cotizar
    una línea; la última cantidad verificada por código manda; y todo código que
    el agente mandó a reemplazar queda excluido de la cotización.
    """
    intenciones: dict[str, int] = {}
    reemplazados: set[str] = set()

    for m in mensajes:
        for tc in getattr(m, "tool_calls", None) or []:
            nombre = tc.get("name")
            args = tc.get("args") or {}
            if nombre == "verificar_stock":
                codigo = _norm(str(args.get("codigo", "")))
                if codigo in CATALOGO:
                    try:
                        cant = max(1, int(args.get("cantidad", 1)))
                    except (TypeError, ValueError):
                        cant = 1
                    intenciones[codigo] = cant
            elif nombre == "buscar_reemplazo":
                reemplazados.add(_norm(str(args.get("codigo", ""))))

    return [
        (c, n)
        for c, n in intenciones.items()
        if c not in reemplazados and CATALOGO[c]["estado"] == "ACTIVO" and CATALOGO[c]["stock"] > 0
    ]


_PATRON_CODIGO = re.compile(r"\b([0-9]{4}-[A-Z0-9]{2,7})\b")


def auditar_propuesta(mensajes) -> str | None:
    """Auditor de restricciones. Devuelve None si la propuesta es admisible, o el
    texto de las objeciones que se le devuelven al agente.

    Es una comprobación, no una receta: enumera qué restricción quedó incumplida
    y deja que el agente decida cómo corregirla (qué producto, qué herramienta,
    en qué orden). Existe porque un modelo pequeño tiende a cerrar la propuesta
    sin haber validado, y sin este rebote los lazos de corrección nunca ocurren.
    """
    herramientas_usadas: set[str] = set()
    for m in mensajes:
        for tc in getattr(m, "tool_calls", None) or []:
            herramientas_usadas.add(tc.get("name"))

    final = ""
    for m in reversed(mensajes):
        if m.__class__.__name__ == "AIMessage" and not (getattr(m, "tool_calls", None) or []):
            final = m.content or ""
            break

    citados = {c for c in _PATRON_CODIGO.findall(final.upper())}
    objeciones: list[str] = []

    inexistentes = sorted(c for c in citados if c not in CATALOGO)
    if inexistentes:
        objeciones.append(
            f"- Citas códigos que no están en el catálogo: {', '.join(inexistentes)}. "
            f"Ningún dato puede salir de tu memoria."
        )

    validos = {c for c in citados if c in CATALOGO}
    no_cotizables = sorted(
        c for c in validos if CATALOGO[c]["estado"] == "DESCONTINUADO" or CATALOGO[c]["stock"] == 0
    )
    if no_cotizables:
        objeciones.append(
            f"- Propones productos no cotizables (descontinuados o sin stock): {', '.join(no_cotizables)}."
        )

    sin_verificar = sorted(c for c in validos if c not in {x for x, _ in extraer_items_de_traza(mensajes)} and c not in no_cotizables)
    if sin_verificar and "verificar_stock" not in herramientas_usadas:
        objeciones.append(
            f"- No verificaste disponibilidad de ninguna línea antes de entregar: {', '.join(sin_verificar)}."
        )

    plataformas = {CATALOGO[c]["plataforma"].split()[0] for c in validos}
    if len(plataformas) > 1:
        objeciones.append(
            f"- Mezclas plataformas incompatibles en una misma configuración: {', '.join(sorted(plataformas))}. "
            f"Los módulos 1756 y 5069 no conviven."
        )

    usa_controllogix = any(CATALOGO[c]["plataforma"].startswith("ControlLogix") for c in validos)
    if usa_controllogix:
        if not any(CATALOGO[c]["familia"] == "chasis" for c in validos):
            objeciones.append("- Propones ControlLogix sin chasis.")
        if not any(CATALOGO[c]["familia"] == "fuente" for c in validos):
            objeciones.append("- Propones ControlLogix sin fuente de alimentación.")
        if "dimensionar_fuente" not in herramientas_usadas:
            objeciones.append("- La fuente propuesta no tiene un dimensionamiento de consumo que la respalde.")
        if "verificar_slots_chasis" not in herramientas_usadas:
            objeciones.append("- El chasis propuesto no tiene una verificación de ocupación que lo respalde.")
    elif validos and "verificar_slots_chasis" not in herramientas_usadas:
        objeciones.append("- No verificaste que los módulos quepan en la expansión local del controlador.")

    if not validos:
        objeciones.append("- Tu respuesta no contiene una lista de materiales con códigos de catálogo.")

    if not objeciones:
        return None

    return (
        "AUDITORÍA DE LA PROPUESTA: no cumple las restricciones y no se puede emitir.\n"
        + "\n".join(objeciones)
        + "\nCorrige la configuración usando las herramientas y vuelve a entregarla."
    )


def emitir_cotizacion(items: list[tuple[str, int]], cliente: str = "Cliente") -> str:
    """Formatea la cotización y aplica IGV 18%. Ruta de código fija, sin LLM."""
    if not items:
        return (
            "\n[COTIZACIÓN] No se registraron líneas validadas en la traza del agente.\n"
            "La capa determinista solo cotiza productos activos y con stock que el agente verificó.\n"
        )

    ancho = 78
    l = [
        "",
        "=" * ancho,
        "JYC AUTOMÁTICA E INSTRUMENTACIÓN S.A.C.".center(ancho),
        "COTIZACIÓN DE TABLERO DE CONTROL ROCKWELL".center(ancho),
        "=" * ancho,
        f"Cliente: {cliente}",
        f"Moneda: {MONEDA}   |   Precios sin IGV   |   Validez: 15 días",
        "-" * ancho,
        f"{'CÓDIGO':<14}{'DESCRIPCIÓN':<38}{'CANT':>5}{'P.UNIT':>10}{'TOTAL':>11}",
        "-" * ancho,
    ]

    subtotal = 0.0
    for codigo, cant in sorted(items):
        p = CATALOGO[codigo]
        importe = p["precio"] * cant
        subtotal += importe
        desc = p["descripcion"]
        desc = desc[:35] + "..." if len(desc) > 38 else desc
        l.append(f"{codigo:<14}{desc:<38}{cant:>5}{p['precio']:>10,.2f}{importe:>11,.2f}")

    igv = round(subtotal * IGV, 2)
    total = round(subtotal + igv, 2)
    l += [
        "-" * ancho,
        f"{'SUBTOTAL':>62}{subtotal:>16,.2f}",
        f"{'IGV (18%)':>62}{igv:>16,.2f}",
        f"{'TOTAL':>62}{total:>16,.2f}",
        "=" * ancho,
        "Nota: subtotal, IGV y formato de esta tabla los genera código determinista,",
        "      no el modelo. La ingeniería de la configuración sí es agéntica.",
        "=" * ancho,
        "",
    ]
    return "\n".join(l)


# ============================================================================
# PERSISTENCIA DE RESULTADOS
# ============================================================================

DIR_COTIZACIONES = Path(__file__).resolve().parent / "cotizaciones"


def _slug(texto: str, max_palabras: int = 6) -> str:
    """Convierte un texto libre en un fragmento de nombre de archivo seguro."""
    limpio = unicodedata.normalize("NFKD", texto or "")
    limpio = limpio.encode("ascii", "ignore").decode("ascii").lower()
    palabras = re.findall(r"[a-z0-9]+", limpio)[:max_palabras]
    return "-".join(palabras) or "cotizacion"


def guardar_resultado(
    requerimiento: str,
    mensajes: list,
    items: list[tuple[str, int]],
    objeciones: str | None,
    cliente: str,
    momento: datetime | None = None,
) -> Path:
    """Escribe la corrida completa en cotizaciones/ como un markdown.

    Guarda el requerimiento, la traza ReAct íntegra, el veredicto del auditor y
    la cotización. Se guarda siempre, también cuando la propuesta fue rechazada:
    una corrida fallida es evidencia del comportamiento del agente, no basura.
    """
    momento = momento or datetime.now()
    DIR_COTIZACIONES.mkdir(parents=True, exist_ok=True)
    ruta = DIR_COTIZACIONES / f"{momento:%Y%m%d_%H%M%S}_{_slug(cliente)}.md"

    traza = [t for t in (formatear_mensaje(m) for m in mensajes) if t]
    estado = "RECHAZADA POR EL AUDITOR" if objeciones else "ADMITIDA"

    doc = [
        f"# Cotización — {cliente}",
        "",
        f"- **Fecha:** {momento:%Y-%m-%d %H:%M:%S}",
        f"- **Modelo:** `{MODELO}` (Ollama local)",
        f"- **Herramientas disponibles:** {len(HERRAMIENTAS)}",
        f"- **Estado de la propuesta:** {estado}",
        f"- **Pasos de la traza:** {len(mensajes)}",
        "",
        "## Requerimiento",
        "",
        requerimiento,
        "",
        "## Traza del agente (ReAct)",
        "",
        "```",
        "\n".join(traza).strip(),
        "```",
        "",
        "## Auditoría de restricciones",
        "",
    ]
    doc += [objeciones] if objeciones else ["Sin objeciones: la propuesta cumple las restricciones verificables."]
    doc += [
        "",
        "## Cotización (capa determinista)",
        "",
        "```",
        emitir_cotizacion(items, cliente).strip(),
        "```",
        "",
    ]

    ruta.write_text("\n".join(doc), encoding="utf-8")
    return ruta


# ============================================================================
# EJECUCIÓN
# ============================================================================

REQUERIMIENTO_DEMO = (
    "Necesito cotizar el tablero de control de una planta de bombeo: 14 motores de 15 HP "
    "con arranque y falla cableados a PLC, 20 señales analógicas de 4-20 mA entre presión, "
    "nivel y caudal, y comunicación EtherNet/IP con el SCADA. El cliente es minero y exige "
    "reserva para crecer. ¿Qué configuración Rockwell me propones?"
)


def _una_accion_por_turno(state: dict) -> dict:
    """Recorta las llamadas paralelas a una sola.

    No decide QUÉ herramienta usar (eso sigue siendo del LLM): solo garantiza el
    ciclo ReAct estricto acción -> observación -> replanteo. Un modelo de 3B
    tiende a disparar 4 herramientas a ciegas y a ignorar lo que devuelven.
    """
    mensajes = state.get("messages") or []
    ultimo = mensajes[-1] if mensajes else None
    llamadas = getattr(ultimo, "tool_calls", None) or []
    if len(llamadas) <= 1:
        return {}
    return {"messages": [ultimo.model_copy(update={"tool_calls": llamadas[:1]})]}


def _observaciones_como_usuario(state: dict) -> dict:
    """Reexpone las observaciones de las herramientas como mensajes de usuario.

    Motivo, verificado sobre la plantilla de llama3.2 en Ollama: las definiciones
    de las herramientas solo se inyectan en el prompt cuando el ÚLTIMO mensaje es
    de rol "user" ({{- if and $.Tools $last }}). En un lazo ReAct, después de
    actuar el último mensaje es de rol "tool", así que a partir del segundo turno
    el modelo pierde de vista que tiene herramientas y trata la observación como
    texto huérfano: responde "no tengo acceso a información en tiempo real".

    Entregar la observación como mensaje de usuario corrige las dos cosas a la
    vez: el dato entra por un canal que el modelo sí atiende, y las herramientas
    vuelven a inyectarse para el turno siguiente.

    Solo cambia lo que ve el LLM (llm_input_messages). El estado conserva los
    ToolMessage reales, que son los que leen el auditor y la capa determinista.
    """
    entrada = []
    for m in state["messages"]:
        if isinstance(m, ToolMessage):
            entrada.append(HumanMessage(content=f"Resultado de {m.name}:\n{m.content}"))
        elif isinstance(m, AIMessage) and m.tool_calls:
            # La acción se narra en texto: si se dejara como tool_calls sin su
            # ToolMessage correspondiente, LangGraph rechazaría el historial.
            acciones = "; ".join(f"{tc['name']}({tc.get('args')})" for tc in m.tool_calls)
            entrada.append(AIMessage(content=f"Consulto: {acciones}"))
        else:
            entrada.append(m)
    return {"llm_input_messages": entrada}


def construir_agente():
    # num_ctx por encima del default de 4096: el system prompt, los esquemas de las
    # 8 herramientas y la traza de observaciones no entran en la ventana por defecto,
    # y Ollama truncaría el inicio en silencio (el agente "olvidaría" sus restricciones).
    llm = ChatOllama(model=MODELO, temperature=0, num_ctx=8192)
    return create_react_agent(
        llm,
        HERRAMIENTAS,
        prompt=cargar_system_prompt(),
        pre_model_hook=_observaciones_como_usuario,
        post_model_hook=_una_accion_por_turno,
    )


def formatear_mensaje(m) -> str:
    """Representación legible de un mensaje de la traza. Cadena vacía si no aporta."""
    tipo = m.__class__.__name__
    if tipo == "HumanMessage":
        contenido = m.content or ""
        etiqueta = "OBJECIÓN DEL AUDITOR" if contenido.startswith("AUDITORÍA") else "REQUERIMIENTO"
        return f"\n>>> {etiqueta}\n{contenido}\n"
    if tipo == "AIMessage":
        if getattr(m, "tool_calls", None):
            return "\n".join(
                f"[PENSAMIENTO -> ACCIÓN] {tc['name']}({tc.get('args')})" for tc in m.tool_calls
            )
        if (m.content or "").strip():
            return f"\n<<< RESPUESTA DEL AGENTE\n{m.content}\n"
    elif tipo == "ToolMessage":
        return f"[OBSERVACIÓN] {m.name}:\n{m.content}\n"
    return ""


def imprimir_mensaje(m) -> None:
    texto = formatear_mensaje(m)
    if texto:
        print(texto)


def main() -> None:
    try:  # consola de Windows: evitar mojibake en acentos
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

    requerimiento = " ".join(sys.argv[1:]).strip() or REQUERIMIENTO_DEMO

    print("=" * 78)
    print("AGENTE COTIZADOR ROCKWELL — JYC Automática e Instrumentación".center(78))
    print(f"Modelo: {MODELO} (Ollama local) | Herramientas: {len(HERRAMIENTAS)}".center(78))
    print("=" * 78)

    agente = construir_agente()
    config = {"recursion_limit": 40}
    entrada: list = [("user", requerimiento)]

    vistos = 0
    mensajes: list = []
    objeciones = None

    for ciclo in range(1, MAX_CICLOS_AUDITORIA + 1):
        try:
            for estado in agente.stream({"messages": entrada}, config=config, stream_mode="values"):
                mensajes = estado["messages"]
                for m in mensajes[vistos:]:
                    imprimir_mensaje(m)
                vistos = len(mensajes)
        except GraphRecursionError:
            print("\n[SISTEMA] El agente agotó el límite de pasos sin cerrar la configuración.\n")

        objeciones = auditar_propuesta(mensajes)
        if objeciones is None:
            break
        if ciclo == MAX_CICLOS_AUDITORIA:
            break
        print(f"[AUDITOR] Propuesta rechazada (ciclo {ciclo}). Se devuelve el control al agente.\n")
        entrada = list(mensajes) + [("user", objeciones)]

    if objeciones:
        print("\n[AUDITOR] La propuesta final sigue incumpliendo restricciones:")
        print(objeciones)

    # ----- Capa determinista: aquí ya no interviene el LLM -----
    cliente = "Proyecto planta de bombeo"
    items = extraer_items_de_traza(mensajes)
    print(emitir_cotizacion(items, cliente))

    ruta = guardar_resultado(requerimiento, mensajes, items, objeciones, cliente)
    print(f"[ARCHIVO] Corrida guardada en: {ruta}\n")


if __name__ == "__main__":
    main()

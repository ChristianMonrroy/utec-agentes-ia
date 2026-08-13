# Agente Cotizador de Tableros Eléctricos Rockwell

Agente **ReAct** que convierte un requerimiento de ingeniería en lenguaje natural
("14 motores de 15 HP, 20 señales 4-20 mA, EtherNet/IP") en una configuración de
tablero de control Rockwell dimensionada y cotizada.

**Autor:** Christian Monrroy — JYC Automática e Instrumentación S.A.C.
**Curso:** Programa de Agentes IA, UTEC Posgrado (MEng. Boris Alzamora)

> ⚠️ **Los datos del catálogo son ficticios.** Los códigos Rockwell son reales, pero
> los precios, consumos de backplane, stock y proyectos del histórico fueron
> generados para este ejercicio académico. No corresponden a listas de precios de
> Rockwell Automation ni a información comercial de JYC.

---

## Agente

| Componente | Naturaleza |
|---|---|
| Selección de plataforma (CompactLogix vs ControlLogix) | Agéntico |
| Dimensionamiento del I/O y conteo de módulos | Agéntico |
| Detección de obsolescencia y de plazos fuera de cronograma, y su corrección | Agéntico |
| Cálculo de IGV (18%), plazo de entrega y formateo de la tabla | **Determinista a propósito** |

El system prompt ([`prompt_sistema.md`](prompt_sistema.md)) define **objetivo y
restricciones**, nunca una secuencia de pasos. Ninguna restricción indica qué
herramienta usar ni en qué orden: eso lo decide el modelo.

## Herramientas (8)

| Herramienta | Función |
|---|---|
| `buscar_catalogo` | Busca productos por texto libre |
| `detalle_producto` | Ficha técnica: canales, consumo, slots, capacidad |
| `verificar_suministro` | Vigencia comercial y plazo de importación contra el cronograma |
| `buscar_reemplazo` | Sucesores para descontinuados o fuera de plazo |
| `dimensionar_fuente` | Consumo de backplane contra capacidad de la fuente |
| `verificar_slots_chasis` | Ocupación de chasis 1756 o expansión local 5069 |
| `calcular` | Aritmética (evaluador AST, sin `eval`) |
| `consultar_historico` | Proyectos previos como referencia de alcance y precio |

## Restricción comercial: JYC importa, no almacena

JYC no mantiene stock; todo el material Rockwell se importa contra pedido. La pregunta
que decide una línea no es cuánto hay, sino **en cuántas semanas llega** y si eso entra
en el cronograma. Por eso el catálogo no tiene existencias: tiene plazo de importación,
y `PLAZO_PROYECTO_SEMANAS` (16 por defecto) es el umbral contra el que se compara.

La cotización final informa la entrega estimada del tablero, determinada por el material
más lento del conjunto.

## Lazos de corrección

El catálogo incluye trampas deliberadas que obligan al agente a replantear:

- **`1756-PA72`** está descontinuada → ya no se puede importar, debe encontrar la
  sucesora `1756-PA75`.
- **`1756-IF8`** es de baja rotación y se importa en 24 semanas, contra las 16 del
  proyecto → debe sustituirlo por el `1756-IF8I`, que llega en 8 pero cuesta USD 670
  más por módulo y consume distinto, invalidando el dimensionamiento de fuente y la
  ocupación de chasis que ya hubiera calculado.

La segunda trampa es la decisión real de un integrador que importa: plazo contra precio.

Además, `auditar_propuesta()` implementa el patrón **evaluator-optimizer**: revisa la
propuesta final contra las restricciones y devuelve el control al agente enumerando
qué incumplió, sin decirle cómo arreglarlo.

## Uso

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

ollama pull qwen2.5:7b
python agente.py
python agente.py "12 motores 10HP, 8 señales 4-20mA, EtherNet/IP"
```

Cada corrida se guarda en [`cotizaciones/`](cotizaciones/) como un markdown con el
requerimiento, la traza ReAct completa, la auditoría y la tabla con IGV.

El modelo por defecto es `qwen2.5:7b` y es configurable sin tocar el código:

```powershell
$env:MODELO_OLLAMA = "llama3.2"; python agente.py
```

La plantilla de chat de llama3.x en Ollama solo inyecta las definiciones de las
herramientas cuando el último mensaje es de rol `user`; en un lazo ReAct el último es
`tool`, así que a partir del segundo turno el modelo deja de ver que tiene
herramientas. El `pre_model_hook` lo compensa reexpresando las observaciones como
mensajes de usuario. Como esa conversión pierde la correspondencia explícita entre
cada llamada y su resultado, solo se aplica a la familia `llama3`; se puede forzar en
cualquier sentido con `$env:REEXPRESAR_OBSERVACIONES = "1"` (o `"0"`).

## Limitación conocida

Con `llama3.2` (3B) el lazo agéntico funciona y el modelo se recupera de sus propios
errores de argumentos, pero no sostiene la planificación de extremo a extremo:
divaga entre validaciones y no converge a una propuesta final. Para completar el
ciclo se requiere un modelo con mejor uso de herramientas (~7B).

## Stack

Python 3.12 · LangChain 1.3 · LangGraph 1.2 (`create_react_agent`) · Ollama

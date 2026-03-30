# Cómo Funciona el Servicio Request Scan

## Descripción General

El Servicio Request Scan es una API web que automatiza la obtención de datos de solicitudes de generación solar desde los portales de las empresas de servicios públicos colombianas **Afinia** (`servicios.energiacaribemar.co`) y **Air-e** (`servicios.air-e.com`). Dado que estos portales son aplicaciones web estándar no diseñadas para acceso programático, el servicio simula una sesión de navegador para navegar por sus formularios y extraer la información.

Todos los trabajos se ejecutan en segundo plano. El servicio responde de inmediato con un `job_id` y luego envía los resultados de forma incremental a una **URL de webhook** que usted provee (en este caso, una aplicación web de Google Apps Script que escribe en Google Sheets).

---

## Los Cuatro Endpoints

| Endpoint | Propósito | Caso de uso |
|---|---|---|
| `POST /scan/range` | Escanea un rango numérico de IDs para descubrir cuáles existen | Descubrimiento diario de nuevas solicitudes |
| `POST /audit/batch` | Obtiene el historial de auditoría para una lista de IDs conocidos | Recuperar el historial de registros existentes |
| `POST /status/refresh` | Verifica cambios de estado en IDs monitoreados | Seguimiento diario de solicitudes activas |
| `POST /fetch/batch` | Re-obtiene los datos completos del formulario para una lista de IDs conocidos | Corrección de registros tras problemas de datos |

También existe un endpoint `GET /health` disponible para pings de mantenimiento de conexión.

---

## Cómo Se Recuperan los Datos (El Flujo de Sesión)

Los portales de las empresas funcionan sobre **ASP.NET WebForms**, que almacena el estado del lado del servidor en una sesión vinculada a una cookie del navegador. Para recuperar los datos de cualquier ID de solicitud, el servicio debe replicar exactamente lo que haría un navegador — en cuatro pasos, todos dentro de la misma sesión:

```
1. ValidaSolicitud        → Confirma que el ID existe
2. Encryptar              → Obtiene un token cifrado y firmado por el servidor para ese ID
3. GET WFSolicitud.aspx   → Carga la página del formulario con el token, inicializando la sesión del servidor
4. CargarDatosSolicitud   → Llama al método de la página que retorna los datos
```

Restricción crítica: **los cuatro pasos deben usar la misma sesión HTTP** (la misma cookie). Si se dividen entre sesiones distintas, el estado del servidor establecido en el paso 3 no está disponible cuando se ejecuta el paso 4, y el servidor devuelve datos vacíos o incorrectos. El servicio crea una **sesión nueva por cada ID** para garantizar este aislamiento.

---

## El Sistema de Eventos Webhook

Cada endpoint recibe un `webhook_url`. A medida que el trabajo en segundo plano avanza, envía eventos JSON estructurados a esa URL:

| Evento | Cuándo se dispara |
|---|---|
| `scan_started` / `audit_started` | El trabajo comienza |
| `item` / `audit_item` | Un ID válido fue procesado exitosamente |
| `item_error` | Un ID falló |
| `status_change` | El estado de un ID monitoreado es diferente al último registrado |
| `scan_finished` / `audit_finished` | El trabajo terminó con un resumen de estadísticas |

Cada evento incluye un `job_id` para que el receptor pueda correlacionar eventos de un trabajo de larga duración.

---

## Estrategias de Escaneo (`/scan/range`)

El endpoint soporta dos estrategias de recorrido:

- **`checkpoint`** (predeterminada): Solo sondea IDs cuyos dos últimos dígitos están en `{10, 30, 50, 70, 90}`. Cuando encuentra un ID válido, expande secuencialmente hasta encontrar el primer ID inválido, y luego salta al siguiente checkpoint. Mucho más rápida para rangos dispersos.
- **`linear`**: Prueba cada ID del rango uno por uno. Usar cuando el rango es denso o cuando se requiere cobertura garantizada.

---

## Restricción de Concurrencia

Los servidores de las empresas tienen un error conocido de concurrencia a nivel de IP: cuando dos o más solicitudes del mismo origen llegan simultáneamente, el servidor puede contaminar el estado en memoria de las sesiones y devolver datos del ID incorrecto. Por esta razón, **solo debe ejecutarse un trabajo a la vez** contra el mismo operador. Ejecutar múltiples llamadas a `/scan/range` o `/fetch/batch` en paralelo producirá datos poco confiables.

---

## Operadores

| Clave del operador | Empresa | Portal |
|---|---|---|
| `"afinia"` | Energía Caribemar (Afinia) | `servicios.energiacaribemar.co` |
| `"aire"` | Air-e | `servicios.air-e.com` |

Air-e requiere configuración TLS adicional (soporte de renegociación heredada y un bundle de CA personalizado) que el servicio maneja automáticamente cuando se especifica `"operator": "aire"`. Al ejecutar trabajos de Air-e de forma local (fuera de Docker), el bundle de CA debe configurarse mediante la variable de entorno `AIRE_CA_BUNDLE`.

---

## Webhook Receptor — Google Apps Script (`/scan/range`)

El webhook es una aplicación web desplegada en **Google Apps Script** que recibe los eventos enviados por el servicio y los escribe como filas en una hoja de cálculo de Google Sheets llamada `RAW`.

### Estructura de la hoja

La hoja `RAW` tiene dos tipos de columnas:

**Columnas fijas** (siempre presentes, en este orden):

| Columna | Contenido |
|---|---|
| `ts` | Marca de tiempo del momento en que llegó el evento |
| `event` | Tipo de evento (`item`, `item_error`, etc.) |
| `job_id` | Identificador único del trabajo que generó el evento |
| `id` | ID de la solicitud procesada |
| `valid` | Si el ID fue encontrado como válido (`true` / `false`) |
| `reason` | Razón del rechazo (si aplica) |
| `error` | Mensaje de error (si aplica) |

**Columnas dinámicas** (a partir de la columna 8): corresponden a los campos del objeto `data` devuelto por el portal (por ejemplo: `CONSECUTIVO`, `ESTADO`, `DESC_ESTADO`, `NOMBRE`, etc.). La última columna dinámica es siempre `raw_json`, que almacena el objeto completo en formato JSON compacto como respaldo.

### Flujo de procesamiento

Cuando llega un evento al webhook, ocurre lo siguiente:

```
1. Se parsea el JSON del cuerpo del request
2. Los eventos scan_started y scan_finished se ignoran (solo retornan "OK")
3. Si la hoja está vacía, se crean las columnas fijas como encabezado
4. Si el evento es "item" con valid=true y data presente:
      → Se extrae el primer registro de data[]
      → Se construye la fila: columnas fijas + columnas dinámicas mapeadas por encabezado
      → Se agrega la fila al final de la hoja
5. Para cualquier otro evento (item_error, etc.):
      → Se escriben solo las columnas fijas
      → Las columnas dinámicas quedan en blanco
      → raw_json almacena data o valida_raw si están disponibles
```

### Conversión de valores

Los valores del campo `data` pasan por una función de conversión antes de escribirse en la hoja:

- **Fechas .NET** con formato `/Date(timestamp)/` → se convierten a objeto `Date` para que Sheets las formatee automáticamente. Las fechas con valor mínimo (`año 0001`) se tratan como vacías.
- **Números** → se escriben tal cual.
- **Texto** → se escribe tal cual.
- **Objetos o arreglos** → se serializan como JSON.
- **`null` o `undefined`** → se escriben como celda vacía.

### Consideraciones importantes

- Los encabezados de las columnas dinámicas **deben estar configurados manualmente** en la fila 1 de la hoja antes de iniciar un escaneo. La función `ensureDynamicHeaders` que los agrega automáticamente está desactivada por defecto para evitar escrituras innecesarias en la hoja.
- Si llega un campo en `data` cuyo nombre no existe como encabezado en la hoja, ese campo **se omite silenciosamente**. El valor completo siempre queda disponible en la columna `raw_json`.
- La hoja de cálculo asociada se identifica por su ID fijo en el código (`openById`). Si la hoja `RAW` no existe, se crea automáticamente.

---

## Webhook Receptor — Google Apps Script (`/status/refresh`)

Este webhook recibe los eventos del endpoint `/status/refresh` y realiza dos acciones simultáneas sobre la hoja de cálculo: **actualiza el estado** en la hoja principal de solicitudes y **registra el historial de auditoría** en una hoja separada de solo adición.

### Hojas involucradas

| Hoja | Propósito |
|---|---|
| `RAW` | Hoja principal de solicitudes. Se actualiza cuando cambia el estado de un ID. |
| `AUDIT` | Registro histórico de auditoría. Solo se agregan filas, nunca se modifican. |

### Eventos procesados

A diferencia del webhook de `/scan/range`, este webhook **solo procesa el evento `status_change`**. Todos los demás eventos (`status_refresh_started`, `status_refresh_finished`, `status_item_error`, etc.) son ignorados y retornan `"IGNORED"`.

Un evento `status_change` llega cuando el servicio detecta que el estado actual de un ID en el portal es diferente al último estado conocido.

### Flujo de procesamiento ante un `status_change`

```
1. Se extrae del evento: id, new_status_text, new_status_code, sheet_row, audit[]

2. Actualización en hoja RAW:
   a. Si se proveyó sheet_row válido → escribe directamente en esa fila (O(1))
   b. Si no → busca la fila recorriendo la columna de IDs y actualiza la primera coincidencia
   En ambos casos se escribe: nuevo estado en columna 11, fecha de actualización en columna 105

3. Registro de auditoría en hoja AUDIT:
   a. Para cada entrada del arreglo audit[] recibido en el evento:
      → Calcula una clave de deduplicación: "{id}|{estado_normalizado}"
      → Si la clave ya existe en la hoja AUDIT, la entrada se omite
      → Si es nueva, se agrega como fila al final de la hoja
   b. Todas las filas nuevas se escriben en un solo batch para eficiencia
```

### Estructura de la hoja AUDIT

| Columna | Campo | Descripción |
|---|---|---|
| `ts` | Marca de tiempo | Momento en que llegó el evento al webhook |
| `id` | ID solicitud | ID de la solicitud |
| `USUARIO` | Usuario | Usuario que realizó la acción en el portal |
| `ID_ACCION` | Código de acción | Código numérico de la acción registrada |
| `FECHA_AUDITORIA` | Fecha | Fecha de la acción (convertida desde formato .NET) |
| `ESTADO_AUDITORIA` | Estado | Estado registrado en esa entrada de auditoría |
| `OBSERVACION_AUDITORIA` | Observación | Texto de observación de la acción |
| `DETALLE_AUDITORIA` | Detalle | Detalle adicional de la acción |
| `raw_json` | JSON completo | Objeto completo de la entrada serializado como JSON |
| `dedupe_key` | Clave de deduplicación | `"{id}\|{estado}"` — siempre la última columna |

### Deduplicación

Para evitar registrar la misma entrada de auditoría múltiples veces (por ejemplo, si `/status/refresh` se ejecuta varias veces sobre el mismo ID), cada fila de auditoría tiene una `dedupe_key` con el formato:

```
{id}|{estado_normalizado_en_minúsculas}
```

Antes de insertar cualquier fila, el webhook carga toda la columna `dedupe_key` en memoria y descarta las entradas cuya clave ya exista. Las filas nuevas se escriben en un único `setValues` al final.

### Estrategia de actualización en RAW (O(1) vs búsqueda)

El servicio puede enviar opcionalmente el campo `sheet_row` en el evento, indicando exactamente en qué fila de la hoja `RAW` está ese ID. Cuando está presente:
- Se escribe directamente en esa fila → **una sola operación de escritura**.

Cuando no está presente o es inválido:
- Se lee toda la columna de IDs de `RAW` y se busca la primera fila que coincida → **más lento**, proporcional al número de registros.

Para máximo rendimiento, se recomienda siempre proveer `sheet_row` en las solicitudes a `/status/refresh`.

---

## Herramienta de Consulta por Transformador — Google Apps Script

Esta herramienta es un script de Google Apps Script independiente del servicio de escaneo. Su propósito es responder la pregunta: **¿qué solicitudes de generación solar están asociadas a una lista de transformadores?**

El usuario sube una lista de códigos de transformador y el script cruza esa lista contra las bases de datos maestras de ambos operadores (Afinia y Air-e), enriquece los resultados con el historial de auditoría y aplica lógica de vencimiento, todo dentro de la misma hoja de cálculo.

### Hojas involucradas

| Hoja | Tipo | Propósito |
|---|---|---|
| `UPLOAD` | Entrada | El usuario pega aquí la lista de códigos de transformador (`COD_TRAFO_PRO`), uno por fila |
| `RESULT` | Salida | El script escribe aquí el resultado del cruce. Se sobreescribe completamente en cada ejecución |

### Fuentes de datos maestras

El script consulta cuatro hojas externas en dos libros de cálculo separados:

| Fuente | Hoja | Operador | Contenido |
|---|---|---|---|
| MASTER 1 | `RAW` | Afinia | Datos principales de solicitudes |
| MASTER 1 | `AUDIT` | Afinia | Historial de auditoría |
| MASTER 2 | `RAW` | Air-e | Datos principales de solicitudes |
| MASTER 2 | `AUDIT` | Air-e | Historial de auditoría |

Estas hojas son alimentadas por los webhooks del servicio de escaneo descritos en las secciones anteriores.

### Cómo ejecutar

1. Pegar los códigos de transformador en la hoja `UPLOAD` (una columna, una por fila, sin encabezado o con cualquier encabezado — el script lo reemplaza automáticamente)
2. Ir al menú **📂 Carga → Procesar archivo + Unir con MASTER**
3. Ingresar la **fecha de corte** en formato `YYYY-MM-DD` cuando el script la solicite
4. El resultado aparece en la hoja `RESULT`

### Flujo de procesamiento

```
1. NORMALIZACIÓN DE UPLOAD
   → Se conserva solo la primera columna
   → El encabezado se fuerza a "COD_TRAFO_PRO"

2. CARGA DE DATOS MAESTROS
   → Se leen las hojas RAW de MASTER 1 y MASTER 2
   → Solo se cargan en memoria las filas cuyo COD_TRAFO_PRO
     aparece en la lista UPLOAD (filtro temprano para eficiencia)
   → Se leen las hojas AUDIT de MASTER 1 y MASTER 2
     (todas las entradas de auditoría, indexadas por id)

3. CRUCE (INNER JOIN)
   → Por cada código de transformador en UPLOAD:
     se buscan todas las solicitudes en los datos maestros
     que tengan ese mismo COD_TRAFO_PRO
   → Solo se incluyen solicitudes que aparezcan en UPLOAD
     (inner join: si no hay coincidencia, la fila se descarta)

4. FILTRO POR FECHA DE CORTE
   → Se eliminan filas cuya FEC_CREA sea posterior a la fecha
     de corte ingresada por el usuario

5. LÓGICA DE VENCIMIENTO
   → Solo para solicitudes en estado:
     "Pendiente documento", "Revisión documento" o "Solicitado"
   → Se calcula: fecha_ultimo_estado + 4 meses = calculated_date
   → Si calculated_date ≤ hoy → expired_flag = "EXPIRED"
   → Si calculated_date > hoy o el estado no aplica → expired_flag vacío

6. EXPANSIÓN POR AUDITORÍA
   → Por cada solicitud resultado del cruce:
     si tiene entradas en AUDIT → se genera una fila por cada entrada
     si no tiene auditoría → se genera una sola fila con las columnas de auditoría vacías
   → Esto significa que una misma solicitud puede aparecer
     múltiples veces en RESULT, una por cada evento de auditoría

7. ESCRITURA EN RESULT
   → Se borra completamente la hoja RESULT
   → Se escribe el resultado en un único setValues (un solo API call)
```

### Columnas del resultado (`RESULT`)

La hoja `RESULT` combina columnas de tres fuentes en este orden:

| Grupo | Columnas | Origen |
|---|---|---|
| Identificador del transformador | `COD_TRAFO_PRO` | Hoja UPLOAD |
| Datos de la solicitud | `id`, `FEC_CREA`, `COD_TRAFO_PRO`, `NOMBRE_CLI`, `EMAIL_CLI`, `DESC_ESTADO`, `TIPO`, `LONGITUD`, `LATITUD`, `DESC_CIUDAD_PRO`, `DESC_CORREGIMIENTO_PRO`, `DIRRECION_PRO`, `POTENCIA_ENTREGADA`, `TENSION_ENTREGADA`, `fecha_ultimo_estado` | MASTER RAW |
| Historial de auditoría | `FECHA_AUDITORIA`, `ESTADO_AUDITORIA`, `OBSERVACION_AUDITORIA` | MASTER AUDIT |
| Calculadas | `calculated_date`, `expired_flag` | Lógica del script |

### Comportamiento ante múltiples registros por transformador

Un mismo código de transformador puede tener **varias solicitudes** asociadas, y cada solicitud puede tener **varios eventos de auditoría**. El resultado se expande en todas las combinaciones posibles:

```
Transformador A → Solicitud 1001 → Auditoría: [Estado X, Estado Y]  →  2 filas
                → Solicitud 1002 → Sin auditoría                    →  1 fila
                                                                    ─────────
                                                          Total:     3 filas
```

---

## Restricción de Alojamiento (Render Plan Gratuito)

El servicio está alojado en el plan gratuito de Render, que apaga el servidor tras **15 minutos de inactividad**. Cualquier trabajo en segundo plano en curso se cancela cuando esto ocurre. Para evitarlo durante trabajos de larga duración, una función de mantenimiento debe hacer ping a `GET /health` cada 10 minutos. Esto se realiza mediante un disparador programado por tiempo en Google Apps Script.

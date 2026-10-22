"""Parámetros de análisis. Ajustar aquí; ningún otro módulo define umbrales."""

# Un intervalo entre dos puntos mayor a esto ya no es un tramo normal.
UMBRAL_HUECO_MINUTOS = 15

# Intervalo largo con extremos separados hasta esta distancia = parada probable.
RADIO_PARADA_KM = 0.10

# Un periodo de movimiento solo cuenta como trayecto si su distancia estimada
# llega a este valor. Debajo de eso se considera ruido de posicionamiento (p. ej. de un vehículo sin
# desplazarse); no se afirma la causa.
DISTANCIA_MINIMA_TRAYECTO_KM = 0.5

# Dos puntos con la misma fecha/hora y a esta distancia o menos (m) son el mismo
# punto repetido (p. ej. GPX solapados). Si están más lejos es un conflicto de timestamp.
TOLERANCIA_DUPLICADO_METROS = 30

# --- Geofencing (etapa 3) ---

# Claves INEGI de las entidades que forman la zona permitida: 09 = Ciudad de México,
# 15 = México (Estado de México).
CLAVES_ENTIDADES_PERMITIDAS = ("09", "15")

# Un punto fuera de la zona pero a esta distancia o menos de su límite exterior se
# clasifica como TOLERANCIA (ruido GPS), no como FUERA.
TOLERANCIA_FRONTERA_METROS = 100

# Una salida solo se confirma con al menos este número de puntos FUERA consecutivos.
# Los puntos en TOLERANCIA no cuentan.
PUNTOS_MINIMOS_FUERA = 2

# --- Revisión de flota (etapa 4; umbrales revisados) ---
# Umbrales operativos adaptados a una flotilla utilizada en plataformas de movilidad (Uber/DiDi).
# Se aplican a la distancia estimada observada de CADA DÍA calendario; el nivel del periodo es el
# máximo nivel diario. Describen volumen de movimiento observado; NO indican anomalía ni incumplimiento.
#   distancia < DISTANCIA_MOVIMIENTO_BAJO_KM                          -> BAJO
#   BAJO_KM <= distancia <= DISTANCIA_MOVIMIENTO_ALTO_KM              -> NORMAL
#   ALTO_KM < distancia <= DISTANCIA_MOVIMIENTO_MUY_ALTO_KM           -> ALTO
#   distancia > DISTANCIA_MOVIMIENTO_MUY_ALTO_KM                      -> MUY ALTO
# BAJO es solo descriptivo: no genera prioridad ni alerta y no implica que 100 km sea un mínimo de trabajo.
DISTANCIA_MOVIMIENTO_BAJO_KM = 100
DISTANCIA_MOVIMIENTO_ALTO_KM = 300
DISTANCIA_MOVIMIENTO_MUY_ALTO_KM = 400

# --- Calidad GPS (etapa 5B) ---
# Solo diagnóstico: ninguno de estos valores modifica prioridad, geofencing ni distancia oficial.

# Un tramo con velocidad media estimada mayor a esto es "velocidad sospechosa" (puede ser real).
VELOCIDAD_SOSPECHOSA_KMH = 160

# Salto GPS probable: un punto aislado X entre A y B que cumple TODAS estas condiciones:
VELOCIDAD_SALTO_GPS_KMH = 300  # el tramo de entrada (A->X) o de regreso (X->B) supera esta velocidad
DISTANCIA_MINIMA_SALTO_KM = 0.30  # X está al menos a esta distancia de A y de B
VENTANA_MAXIMA_SALTO_SEGUNDOS = 120  # de A a B transcurre como máximo este tiempo
RELACION_DESVIO_MINIMA = 4.0  # (A->X + X->B) / (A->B) al menos esto
# Además, A->B debe ser físicamente plausible (velocidad A-B <= VELOCIDAD_SOSPECHOSA_KMH).

# --- BAANOOL (etapa 6A) ---
# Tiempo máximo de espera de cada petición HTTP a BAANOOL (exportación y descarga).
TIMEOUT_BAANOOL_SEGUNDOS = 30

# Zonas horarias (etapa 6A). El usuario siempre trabaja en hora local; los parámetros startTime/endTime
# del endpoint de exportación de BAANOOL se interpretan en la hora del backend (UTC+8).
# Los timestamps DENTRO de los GPX descargados NO se convierten.
ZONA_HORARIA_LOCAL = "America/Mexico_City"
ZONA_HORARIA_BAANOOL = "Asia/Shanghai"

# --- Descarga por lote desde BAANOOL (etapa 6B-2) ---
# Pausa entre vehículos (solo después de uno que hizo peticiones a BAANOOL). Secuencial y conservador.
PAUSA_ENTRE_DESCARGAS_SEGUNDOS = 2

# FM Vida 92.5 → Instagram

Automatización gratuita para detectar noticias nuevas en
`fmvidafortinolmos.com.ar`, generar una placa institucional de 1080 × 1350 px y
publicarla en una cuenta profesional de Instagram mediante la API oficial de
Meta.

## Seguridad inicial

La primera ejecución programada **no publica noticias existentes**. Solo guarda
una referencia de las noticias visibles en ese momento. Las publicaciones
automáticas comienzan con las noticias que aparezcan después.

Las credenciales nunca se escriben en el código. Deben guardarse como secretos
del repositorio con los nombres:

- `IG_USER_ID`
- `IG_ACCESS_TOKEN`

## Crear el repositorio

1. Ingresar en GitHub con `fmvida925-ai`.
2. Crear un repositorio llamado `fmvida-instagram-auto`.
3. Elegir **Public**.
4. No agregar README, `.gitignore` ni licencia desde GitHub.
5. Subir todo el contenido de este paquete conservando sus carpetas.

## Prueba segura

1. Abrir la pestaña **Actions** del repositorio.
2. Seleccionar **Publicar noticias en Instagram**.
3. Pulsar **Run workflow**.
4. Mantener activado `solo_prueba`.

La prueba genera una placa, pero no intenta publicar en Instagram.

## Activación

La activación se realiza después de crear la aplicación de Meta, obtener el ID
de la cuenta profesional y guardar los dos secretos. El flujo se ejecuta a los
minutos 7 y 37 de cada hora.

## Personalización

Los textos generales y hashtags están en `config.json`. El diseño se encuentra
en la función `generate_card` de `scripts/fmvida_auto.py`. El logo institucional
está en `assets/logo.png`.


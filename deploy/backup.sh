#!/bin/bash
# Резервная копия ИИ-агента по регламентам: база PostgreSQL (регламенты, граф, разъяснения службы качества, журнал
# запросов, пользователи) + исходные файлы загруженных регламентов. Копия проверяется (pg_restore -l), старые
# удаляются. Самое ценное — разъяснения службы качества и журнал: их больше нигде нет; регламенты можно загрузить
# заново из DOCX. Секреты (app.env) не копируются — они хранятся отдельно (см. docs/OPERATIONS.md).
#
# Запуск от пользователя службы (peer-доступ к БД): sudo -u stand deploy/backup.sh
# Переменные: DB (corpdocs), BACKUP_DIR (/srv/stand/backup), BACKUP_KEEP_DAYS (14), APP_DIR (/srv/stand/app),
#             UPLOAD_DIR (data/uploads относительно APP_DIR).
# Восстановление: docs/OPERATIONS.md, раздел «Восстановление из резервной копии».
set -euo pipefail
DB=${DB:-corpdocs}
DIR=${BACKUP_DIR:-/srv/stand/backup}
KEEP=${BACKUP_KEEP_DAYS:-14}
APP_DIR=${APP_DIR:-/srv/stand/app}
UPLOADS=${UPLOAD_DIR:-data/uploads}
[[ "$UPLOADS" = /* ]] || UPLOADS="$APP_DIR/$UPLOADS"

umask 077                                   # в копии — персональные данные сотрудников: доступ только владельцу
mkdir -p "$DIR"
ts=$(date +%F-%H%M)
dump="$DIR/$DB-$ts.dump"
pg_dump -Fc --no-owner "$DB" > "$dump.part" && mv "$dump.part" "$dump"
pg_restore -l "$dump" > /dev/null           # копия читается — иначе ошибка и код возврата ≠ 0
if [ -d "$UPLOADS" ]; then
  tar czf "$DIR/uploads-$ts.tgz" -C "$(dirname "$UPLOADS")" "$(basename "$UPLOADS")"
fi
find "$DIR" -maxdepth 1 \( -name "$DB-*.dump" -o -name "uploads-*.tgz" \) -mtime +"$KEEP" -delete
echo "резервная копия: $dump ($(du -h "$dump" | cut -f1)); хранится $KEEP дн.; копий: $(ls "$DIR"/"$DB"-*.dump | wc -l)"

-- Migração: Adicionar coluna profile_image_url à tabela usuarios
-- Execute este script se o banco já existir

-- SQLite (descomente e execute):
-- ALTER TABLE usuarios ADD COLUMN profile_image_url VARCHAR(500);

-- PostgreSQL (descomente e execute):
-- ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS profile_image_url VARCHAR(500);

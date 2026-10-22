ALTER TABLE forge_projects
    DROP CONSTRAINT IF EXISTS forge_projects_side_quest_defaults_check;
-- The previous project decoder forbids unknown fields. Preserve every other
-- document field and all row metadata when returning to that decoder.
UPDATE forge_projects
SET document = document - 'side_quest_defaults'
WHERE document ? 'side_quest_defaults';

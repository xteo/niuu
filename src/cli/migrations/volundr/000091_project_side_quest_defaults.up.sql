-- Missing defaults retain legacy resolution; JSON null explicitly clears them.
-- Keep this in the existing project document, without backfilling either state.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'forge_projects_side_quest_defaults_check'
          AND conrelid = 'forge_projects'::regclass
    ) THEN
        ALTER TABLE forge_projects
            ADD CONSTRAINT forge_projects_side_quest_defaults_check
            CHECK (
                CASE
                    WHEN NOT (document ? 'side_quest_defaults') THEN TRUE
                    WHEN document->'side_quest_defaults' = 'null'::jsonb THEN TRUE
                    WHEN jsonb_typeof(document->'side_quest_defaults') = 'object' THEN
                        COALESCE(
                            (document->'side_quest_defaults') ?& ARRAY['host', 'local_path']
                            AND (document->'side_quest_defaults') - ARRAY['host', 'local_path']
                                = '{}'::jsonb
                            AND jsonb_typeof(document->'side_quest_defaults'->'host') = 'string'
                            AND length(document->'side_quest_defaults'->>'host') BETWEEN 1 AND 100
                            AND document->'side_quest_defaults'->>'host' ~ '^[a-zA-Z0-9_.-]+$'
                            AND jsonb_typeof(document->'side_quest_defaults'->'local_path') = 'string'
                            AND length(document->'side_quest_defaults'->>'local_path')
                                BETWEEN 1 AND 2048
                            AND document->'side_quest_defaults'->>'local_path' LIKE '/%'
                            AND strpos(document->'side_quest_defaults'->>'local_path', chr(92)) = 0
                            AND (document->'side_quest_defaults'->>'local_path') COLLATE "C"
                                !~ '[[:cntrl:]]',
                            FALSE
                        )
                    ELSE FALSE
                END
            );
    END IF;
END $$;

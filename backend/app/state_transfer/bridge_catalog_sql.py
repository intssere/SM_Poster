"""SELECT-only expression mirroring the pinned Python catalog descriptor."""

def normalized(expression):
    return f"regexp_replace(btrim({expression}), '\\s+', ' ', 'g')"


def default(expression):
    return f"""(WITH RECURSIVE d(s) AS (
      SELECT {normalized(expression)}
      UNION ALL SELECT btrim(substr(s,2,length(s)-2)) FROM d
      WHERE left(s,1)='(' AND right(s,1)=')'
    ) SELECT regexp_replace(s,
      '::(character varying|integer|bigint|numeric|boolean|date|timestamp with time zone)$','')
      FROM d WHERE s IS NULL OR NOT(left(s,1)='(' AND right(s,1)=')') LIMIT 1)"""


def descriptor():
    # e.name is a fixed policy table. All source enum/security/trigger evidence
    # matches catalog.contract; PG18 redundant NOT NULL rows are checked outside.
    return f"""jsonb_build_object(
      'catalog',jsonb_build_object(
        'columns',COALESCE((SELECT jsonb_agg(jsonb_build_object(
          'name',c.column_name,'type',c.data_type,'udt',c.udt_name,
          'length',c.character_maximum_length,'precision',c.numeric_precision,
          'scale',c.numeric_scale,'datetime_precision',c.datetime_precision,
          'nullable',c.is_nullable='YES','default',{default('c.column_default')},
          'collation',c.collation_name,'identity',c.is_identity='YES',
          'identity_generation',c.identity_generation,'generated',c.is_generated,
          'generation_expression',{normalized('c.generation_expression')})
          ORDER BY c.ordinal_position)
          FROM information_schema.columns c
          WHERE c.table_schema='public' AND c.table_name=e.name),'[]'::jsonb),
        'constraints',COALESCE((SELECT jsonb_agg(jsonb_build_object(
          'name',c.conname,'kind',c.contype,
          'definition',{normalized('pg_get_constraintdef(c.oid,true)')},
          'deferrable',c.condeferrable,'deferred',c.condeferred,'validated',c.convalidated)
          ORDER BY c.conname COLLATE "C")
          FROM pg_constraint c WHERE c.conrelid=to_regclass('public.'||e.name)
          AND c.contype<>'n'),'[]'::jsonb),
        'indexes',COALESCE((SELECT jsonb_agg(jsonb_build_object(
          'name',idx.relname,'unique',i.indisunique,'valid',i.indisvalid,
          'ready',i.indisready,'nulls_not_distinct',i.indnullsnotdistinct,
          'key_columns',i.indnkeyatts,'columns',i.indnatts,'access_method',am.amname,
          'definition',{normalized('pg_get_indexdef(i.indexrelid)')},
          'predicate',{normalized('pg_get_expr(i.indpred,i.indrelid)')})
          ORDER BY idx.relname COLLATE "C")
          FROM pg_index i JOIN pg_class idx ON idx.oid=i.indexrelid
          JOIN pg_am am ON am.oid=idx.relam
          WHERE i.indrelid=to_regclass('public.'||e.name)),'[]'::jsonb)),
      'security',(SELECT jsonb_build_object('relkind',c.relkind,
          'relrowsecurity',c.relrowsecurity,'relforcerowsecurity',c.relforcerowsecurity)
          FROM pg_class c WHERE c.oid=to_regclass('public.'||e.name)),
      'triggers',COALESCE((SELECT jsonb_agg(jsonb_build_object(
          'tgname',t.tgname,'tgenabled',t.tgenabled,
          'definition',pg_get_triggerdef(t.oid),
          'function_definition',pg_get_functiondef(t.tgfoid)) ORDER BY t.tgname COLLATE "C")
          FROM pg_trigger t WHERE t.tgrelid=to_regclass('public.'||e.name)
          AND NOT t.tgisinternal),'[]'::jsonb))
      || CASE WHEN EXISTS(SELECT 1 FROM pg_attribute a JOIN pg_enum v ON v.enumtypid=a.atttypid
          WHERE a.attrelid=to_regclass('public.'||e.name) AND NOT a.attisdropped)
      THEN jsonb_build_object('enum_labels',(SELECT jsonb_agg(jsonb_build_array(a.attname,v.enumlabel)
          ORDER BY a.attname COLLATE "C",v.enumsortorder)
          FROM pg_attribute a JOIN pg_enum v ON v.enumtypid=a.atttypid
          WHERE a.attrelid=to_regclass('public.'||e.name) AND NOT a.attisdropped))
      ELSE '{{}}'::jsonb END"""


def constraints_guard():
    return """NOT EXISTS(
      SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid
      JOIN pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname='public' AND (
        NOT COALESCE((to_jsonb(c)->>'conenforced')::boolean,true) OR
        (c.contype='n' AND (NOT c.convalidated OR c.condeferrable OR c.condeferred
          OR c.connoinherit OR cardinality(c.conkey)<>1 OR
          (SELECT count(*) FROM pg_attribute a WHERE a.attrelid=c.conrelid
           AND a.attnum=ANY(c.conkey) AND a.attnotnull AND NOT a.attisdropped)<>1))
      ))"""
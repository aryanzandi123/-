--
-- PostgreSQL database dump
--

\restrict sdM9UxWXWPvrp02adm5C7VZvfWhQMLOfN89jWLJ41qviqalf1e4wepL3NoeWU2Z

-- Dumped from database version 17.9 (Debian 17.9-1.pgdg13+1)
-- Dumped by pg_dump version 18.3 (Homebrew)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: amcheck; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS amcheck WITH SCHEMA public;


--
-- Name: EXTENSION amcheck; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION amcheck IS 'functions for verifying relation integrity';


--
-- Name: pageinspect; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pageinspect WITH SCHEMA public;


--
-- Name: EXTENSION pageinspect; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pageinspect IS 'inspect the contents of database pages at a low level';


--
-- Name: pg_stat_statements; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA public;


--
-- Name: EXTENSION pg_stat_statements; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_stat_statements IS 'track planning and execution statistics of all SQL statements executed';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


--
-- Name: chain_participants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chain_participants (
    chain_id integer NOT NULL,
    interaction_id integer NOT NULL,
    role character varying(30) DEFAULT 'hop'::character varying NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT valid_chain_participant_role CHECK (((role)::text = ANY ((ARRAY['origin'::character varying, 'hop'::character varying, 'net_effect'::character varying])::text[])))
);


--
-- Name: indirect_chains; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.indirect_chains (
    id integer NOT NULL,
    chain_proteins jsonb NOT NULL,
    origin_interaction_id integer NOT NULL,
    pathway_name character varying(200),
    pathway_id integer,
    chain_with_arrows jsonb,
    discovered_in_query character varying(50),
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone,
    chain_signature character varying(32) DEFAULT ''::character varying NOT NULL
);


--
-- Name: indirect_chains_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.indirect_chains_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: indirect_chains_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.indirect_chains_id_seq OWNED BY public.indirect_chains.id;


--
-- Name: interaction_claims; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.interaction_claims (
    id integer NOT NULL,
    interaction_id integer NOT NULL,
    function_name text NOT NULL,
    arrow character varying(50),
    interaction_effect character varying(50),
    direction character varying(30),
    mechanism text,
    effect_description text,
    biological_consequences jsonb DEFAULT '[]'::jsonb,
    specific_effects jsonb DEFAULT '[]'::jsonb,
    evidence jsonb DEFAULT '[]'::jsonb,
    pmids jsonb DEFAULT '[]'::jsonb,
    pathway_name character varying(200),
    pathway_id integer,
    confidence numeric(3,2),
    function_context character varying(20),
    context_data jsonb,
    source_query character varying(50),
    discovery_method character varying(50),
    raw_function_data jsonb,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    chain_id integer,
    CONSTRAINT valid_claim_confidence_range CHECK (((confidence IS NULL) OR ((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))))
);


--
-- Name: interaction_claims_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.interaction_claims_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: interaction_claims_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.interaction_claims_id_seq OWNED BY public.interaction_claims.id;


--
-- Name: interactions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.interactions (
    id integer NOT NULL,
    protein_a_id integer NOT NULL,
    protein_b_id integer NOT NULL,
    confidence numeric(3,2),
    direction character varying(20) NOT NULL,
    arrow character varying(50),
    data jsonb DEFAULT '{}'::jsonb NOT NULL,
    discovered_in_query character varying(50),
    discovery_method character varying(50),
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    interaction_type character varying(100),
    upstream_interactor character varying(50),
    mediator_chain jsonb,
    depth integer DEFAULT 1 NOT NULL,
    chain_context jsonb,
    function_context character varying(20),
    arrows jsonb,
    chain_with_arrows jsonb,
    chain_id integer,
    CONSTRAINT interaction_proteins_different CHECK ((protein_a_id <> protein_b_id)),
    CONSTRAINT no_bidirectional_direction CHECK (((direction)::text = ANY ((ARRAY['a_to_b'::character varying, 'b_to_a'::character varying, 'main_to_primary'::character varying, 'primary_to_main'::character varying])::text[]))),
    CONSTRAINT valid_confidence_range CHECK (((confidence IS NULL) OR ((confidence >= (0)::numeric) AND (confidence <= (1)::numeric)))),
    CONSTRAINT valid_function_context CHECK (((function_context IS NULL) OR ((function_context)::text = ANY ((ARRAY['direct'::character varying, 'net'::character varying, 'chain_derived'::character varying, 'mixed'::character varying])::text[])))),
    CONSTRAINT valid_interaction_type CHECK (((interaction_type IS NULL) OR ((interaction_type)::text = ANY ((ARRAY['direct'::character varying, 'indirect'::character varying])::text[]))))
);


--
-- Name: interactions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.interactions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: interactions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.interactions_id_seq OWNED BY public.interactions.id;


--
-- Name: pathway_interactions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pathway_interactions (
    id integer NOT NULL,
    pathway_id integer NOT NULL,
    interaction_id integer NOT NULL,
    assignment_confidence numeric(3,2),
    assignment_method character varying(50),
    created_at timestamp without time zone DEFAULT now() NOT NULL
);


--
-- Name: pathway_interactions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.pathway_interactions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: pathway_interactions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.pathway_interactions_id_seq OWNED BY public.pathway_interactions.id;


--
-- Name: pathway_parents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pathway_parents (
    id integer NOT NULL,
    child_pathway_id integer NOT NULL,
    parent_pathway_id integer NOT NULL,
    relationship_type character varying(30) NOT NULL,
    confidence numeric(3,2) NOT NULL,
    source character varying(20),
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT no_self_parent CHECK ((child_pathway_id <> parent_pathway_id))
);


--
-- Name: pathway_parents_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.pathway_parents_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: pathway_parents_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.pathway_parents_id_seq OWNED BY public.pathway_parents.id;


--
-- Name: pathways; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pathways (
    id integer NOT NULL,
    name character varying(200) NOT NULL,
    description text,
    ontology_id character varying(50),
    ontology_source character varying(20),
    canonical_term character varying(200),
    ai_generated boolean NOT NULL,
    usage_count integer NOT NULL,
    extra_data jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    is_leaf boolean DEFAULT true NOT NULL,
    hierarchy_level integer DEFAULT 0 NOT NULL,
    protein_count integer DEFAULT 0 NOT NULL,
    ancestor_ids jsonb DEFAULT '[]'::jsonb NOT NULL
);


--
-- Name: pathways_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.pathways_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: pathways_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.pathways_id_seq OWNED BY public.pathways.id;


--
-- Name: protein_aliases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.protein_aliases (
    id integer NOT NULL,
    alias_symbol character varying(100) NOT NULL,
    protein_id integer NOT NULL,
    source character varying(32) NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL
);


--
-- Name: protein_aliases_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.protein_aliases_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: protein_aliases_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.protein_aliases_id_seq OWNED BY public.protein_aliases.id;


--
-- Name: proteins; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.proteins (
    id integer NOT NULL,
    symbol character varying(50) NOT NULL,
    first_queried timestamp without time zone DEFAULT now() NOT NULL,
    last_queried timestamp without time zone DEFAULT now() NOT NULL,
    query_count integer NOT NULL,
    total_interactions integer NOT NULL,
    extra_data jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    pipeline_status character varying(20) DEFAULT 'idle'::character varying,
    last_pipeline_phase character varying(50)
);


--
-- Name: proteins_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.proteins_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: proteins_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.proteins_id_seq OWNED BY public.proteins.id;


--
-- Name: indirect_chains id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.indirect_chains ALTER COLUMN id SET DEFAULT nextval('public.indirect_chains_id_seq'::regclass);


--
-- Name: interaction_claims id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interaction_claims ALTER COLUMN id SET DEFAULT nextval('public.interaction_claims_id_seq'::regclass);


--
-- Name: interactions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interactions ALTER COLUMN id SET DEFAULT nextval('public.interactions_id_seq'::regclass);


--
-- Name: pathway_interactions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pathway_interactions ALTER COLUMN id SET DEFAULT nextval('public.pathway_interactions_id_seq'::regclass);


--
-- Name: pathway_parents id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pathway_parents ALTER COLUMN id SET DEFAULT nextval('public.pathway_parents_id_seq'::regclass);


--
-- Name: pathways id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pathways ALTER COLUMN id SET DEFAULT nextval('public.pathways_id_seq'::regclass);


--
-- Name: protein_aliases id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.protein_aliases ALTER COLUMN id SET DEFAULT nextval('public.protein_aliases_id_seq'::regclass);


--
-- Name: proteins id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proteins ALTER COLUMN id SET DEFAULT nextval('public.proteins_id_seq'::regclass);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: indirect_chains chain_origin_signature_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.indirect_chains
    ADD CONSTRAINT chain_origin_signature_unique UNIQUE (origin_interaction_id, chain_signature);


--
-- Name: chain_participants chain_participants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chain_participants
    ADD CONSTRAINT chain_participants_pkey PRIMARY KEY (chain_id, interaction_id);


--
-- Name: indirect_chains indirect_chains_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.indirect_chains
    ADD CONSTRAINT indirect_chains_pkey PRIMARY KEY (id);


--
-- Name: interaction_claims interaction_claims_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interaction_claims
    ADD CONSTRAINT interaction_claims_pkey PRIMARY KEY (id);


--
-- Name: interactions interaction_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interactions
    ADD CONSTRAINT interaction_unique UNIQUE (protein_a_id, protein_b_id);


--
-- Name: interactions interactions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interactions
    ADD CONSTRAINT interactions_pkey PRIMARY KEY (id);


--
-- Name: pathway_interactions pathway_interaction_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pathway_interactions
    ADD CONSTRAINT pathway_interaction_unique UNIQUE (pathway_id, interaction_id);


--
-- Name: pathway_interactions pathway_interactions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pathway_interactions
    ADD CONSTRAINT pathway_interactions_pkey PRIMARY KEY (id);


--
-- Name: pathway_parents pathway_parent_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pathway_parents
    ADD CONSTRAINT pathway_parent_unique UNIQUE (child_pathway_id, parent_pathway_id);


--
-- Name: pathway_parents pathway_parents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pathway_parents
    ADD CONSTRAINT pathway_parents_pkey PRIMARY KEY (id);


--
-- Name: pathways pathways_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pathways
    ADD CONSTRAINT pathways_pkey PRIMARY KEY (id);


--
-- Name: protein_aliases protein_aliases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.protein_aliases
    ADD CONSTRAINT protein_aliases_pkey PRIMARY KEY (id);


--
-- Name: proteins proteins_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proteins
    ADD CONSTRAINT proteins_pkey PRIMARY KEY (id);


--
-- Name: idx_chain_participants_interaction; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chain_participants_interaction ON public.chain_participants USING btree (interaction_id);


--
-- Name: idx_claims_arrow; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_claims_arrow ON public.interaction_claims USING btree (arrow);


--
-- Name: idx_claims_chain; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_claims_chain ON public.interaction_claims USING btree (chain_id);


--
-- Name: idx_claims_chain_pathway; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_claims_chain_pathway ON public.interaction_claims USING btree (chain_id, pathway_id);


--
-- Name: idx_claims_evidence_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_claims_evidence_gin ON public.interaction_claims USING gin (evidence);


--
-- Name: idx_claims_function_context; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_claims_function_context ON public.interaction_claims USING btree (function_context);


--
-- Name: idx_claims_interaction; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_claims_interaction ON public.interaction_claims USING btree (interaction_id);


--
-- Name: idx_claims_interaction_context; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_claims_interaction_context ON public.interaction_claims USING btree (interaction_id, function_context);


--
-- Name: idx_claims_pathway; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_claims_pathway ON public.interaction_claims USING btree (pathway_id);


--
-- Name: idx_claims_pmids_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_claims_pmids_gin ON public.interaction_claims USING gin (pmids);


--
-- Name: idx_claims_source_query; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_claims_source_query ON public.interaction_claims USING btree (source_query);


--
-- Name: idx_indirect_chains_chain_signature; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_indirect_chains_chain_signature ON public.indirect_chains USING btree (chain_signature);


--
-- Name: idx_interaction_discovered_in; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_interaction_discovered_in ON public.interactions USING btree (discovered_in_query);


--
-- Name: idx_interaction_pair_context; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_interaction_pair_context ON public.interactions USING btree (protein_a_id, protein_b_id, function_context);


--
-- Name: idx_interaction_pair_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_interaction_pair_lookup ON public.interactions USING btree (protein_a_id, protein_b_id);


--
-- Name: idx_interactions_chain_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_interactions_chain_id ON public.interactions USING btree (chain_id);


--
-- Name: idx_interactions_data_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_interactions_data_gin ON public.interactions USING gin (data);


--
-- Name: idx_interactions_depth; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_interactions_depth ON public.interactions USING btree (depth);


--
-- Name: idx_interactions_function_context; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_interactions_function_context ON public.interactions USING btree (function_context);


--
-- Name: idx_interactions_interaction_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_interactions_interaction_type ON public.interactions USING btree (interaction_type);


--
-- Name: idx_interactions_upstream; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_interactions_upstream ON public.interactions USING btree (upstream_interactor);


--
-- Name: idx_pathway_parents_child; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pathway_parents_child ON public.pathway_parents USING btree (child_pathway_id);


--
-- Name: idx_pathway_parents_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pathway_parents_parent ON public.pathway_parents USING btree (parent_pathway_id);


--
-- Name: idx_pathways_hierarchy_level; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pathways_hierarchy_level ON public.pathways USING btree (hierarchy_level);


--
-- Name: idx_pathways_is_leaf; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pathways_is_leaf ON public.pathways USING btree (is_leaf);


--
-- Name: idx_pathways_ontology; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pathways_ontology ON public.pathways USING btree (ontology_source, ontology_id);


--
-- Name: ix_indirect_chains_origin_interaction_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_indirect_chains_origin_interaction_id ON public.indirect_chains USING btree (origin_interaction_id);


--
-- Name: ix_interaction_claims_interaction_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_interaction_claims_interaction_id ON public.interaction_claims USING btree (interaction_id);


--
-- Name: ix_interaction_claims_pathway_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_interaction_claims_pathway_id ON public.interaction_claims USING btree (pathway_id);


--
-- Name: ix_interactions_confidence; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_interactions_confidence ON public.interactions USING btree (confidence);


--
-- Name: ix_interactions_protein_a_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_interactions_protein_a_id ON public.interactions USING btree (protein_a_id);


--
-- Name: ix_interactions_protein_b_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_interactions_protein_b_id ON public.interactions USING btree (protein_b_id);


--
-- Name: ix_pathway_interactions_interaction_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pathway_interactions_interaction_id ON public.pathway_interactions USING btree (interaction_id);


--
-- Name: ix_pathway_interactions_pathway_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pathway_interactions_pathway_id ON public.pathway_interactions USING btree (pathway_id);


--
-- Name: ix_pathway_parents_child_pathway_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pathway_parents_child_pathway_id ON public.pathway_parents USING btree (child_pathway_id);


--
-- Name: ix_pathway_parents_parent_pathway_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pathway_parents_parent_pathway_id ON public.pathway_parents USING btree (parent_pathway_id);


--
-- Name: ix_pathways_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_pathways_name ON public.pathways USING btree (name);


--
-- Name: ix_protein_aliases_alias_symbol; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_protein_aliases_alias_symbol ON public.protein_aliases USING btree (alias_symbol);


--
-- Name: ix_protein_aliases_protein_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_protein_aliases_protein_id ON public.protein_aliases USING btree (protein_id);


--
-- Name: ix_proteins_pipeline_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proteins_pipeline_status ON public.proteins USING btree (pipeline_status);


--
-- Name: ix_proteins_symbol; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_proteins_symbol ON public.proteins USING btree (symbol);


--
-- Name: uq_claim_interaction_fn_pw_ctx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_claim_interaction_fn_pw_ctx ON public.interaction_claims USING btree (interaction_id, function_name, COALESCE(pathway_name, ''::character varying), COALESCE(function_context, ''::character varying), COALESCE(chain_id, 0));


--
-- Name: chain_participants chain_participants_chain_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chain_participants
    ADD CONSTRAINT chain_participants_chain_id_fkey FOREIGN KEY (chain_id) REFERENCES public.indirect_chains(id) ON DELETE CASCADE;


--
-- Name: chain_participants chain_participants_interaction_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chain_participants
    ADD CONSTRAINT chain_participants_interaction_id_fkey FOREIGN KEY (interaction_id) REFERENCES public.interactions(id) ON DELETE CASCADE;


--
-- Name: indirect_chains indirect_chains_origin_interaction_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.indirect_chains
    ADD CONSTRAINT indirect_chains_origin_interaction_id_fkey FOREIGN KEY (origin_interaction_id) REFERENCES public.interactions(id) ON DELETE CASCADE;


--
-- Name: indirect_chains indirect_chains_pathway_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.indirect_chains
    ADD CONSTRAINT indirect_chains_pathway_id_fkey FOREIGN KEY (pathway_id) REFERENCES public.pathways(id) ON DELETE SET NULL;


--
-- Name: interaction_claims interaction_claims_chain_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interaction_claims
    ADD CONSTRAINT interaction_claims_chain_id_fkey FOREIGN KEY (chain_id) REFERENCES public.indirect_chains(id) ON DELETE SET NULL;


--
-- Name: interaction_claims interaction_claims_interaction_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interaction_claims
    ADD CONSTRAINT interaction_claims_interaction_id_fkey FOREIGN KEY (interaction_id) REFERENCES public.interactions(id) ON DELETE CASCADE;


--
-- Name: interaction_claims interaction_claims_pathway_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interaction_claims
    ADD CONSTRAINT interaction_claims_pathway_id_fkey FOREIGN KEY (pathway_id) REFERENCES public.pathways(id) ON DELETE SET NULL;


--
-- Name: interactions interactions_chain_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interactions
    ADD CONSTRAINT interactions_chain_id_fkey FOREIGN KEY (chain_id) REFERENCES public.indirect_chains(id) ON DELETE SET NULL;


--
-- Name: interactions interactions_protein_a_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interactions
    ADD CONSTRAINT interactions_protein_a_id_fkey FOREIGN KEY (protein_a_id) REFERENCES public.proteins(id) ON DELETE CASCADE;


--
-- Name: interactions interactions_protein_b_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interactions
    ADD CONSTRAINT interactions_protein_b_id_fkey FOREIGN KEY (protein_b_id) REFERENCES public.proteins(id) ON DELETE CASCADE;


--
-- Name: pathway_interactions pathway_interactions_interaction_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pathway_interactions
    ADD CONSTRAINT pathway_interactions_interaction_id_fkey FOREIGN KEY (interaction_id) REFERENCES public.interactions(id) ON DELETE CASCADE;


--
-- Name: pathway_interactions pathway_interactions_pathway_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pathway_interactions
    ADD CONSTRAINT pathway_interactions_pathway_id_fkey FOREIGN KEY (pathway_id) REFERENCES public.pathways(id) ON DELETE CASCADE;


--
-- Name: pathway_parents pathway_parents_child_pathway_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pathway_parents
    ADD CONSTRAINT pathway_parents_child_pathway_id_fkey FOREIGN KEY (child_pathway_id) REFERENCES public.pathways(id) ON DELETE CASCADE;


--
-- Name: pathway_parents pathway_parents_parent_pathway_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pathway_parents
    ADD CONSTRAINT pathway_parents_parent_pathway_id_fkey FOREIGN KEY (parent_pathway_id) REFERENCES public.pathways(id) ON DELETE CASCADE;


--
-- Name: protein_aliases protein_aliases_protein_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.protein_aliases
    ADD CONSTRAINT protein_aliases_protein_id_fkey FOREIGN KEY (protein_id) REFERENCES public.proteins(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict sdM9UxWXWPvrp02adm5C7VZvfWhQMLOfN89jWLJ41qviqalf1e4wepL3NoeWU2Z


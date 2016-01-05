
import os
import re
import sys
import glob
import gzip
import json
import time
import errno
import shutil
import urllib
import logging
import tarfile 
import urllib2
import requests
import feedparser # For LOVD atom data 

from Bio import Entrez, SeqIO
from appdirs import *

# Importing: https://bitbucket.org/biocommons/hgvs
import hgvs as hgvs_biocommons
import hgvs.parser as hgvs_biocommons_parser
import hgvs.dataproviders.uta as hgvs_biocommons_uta # http://hgvs.readthedocs.org/en/latest/examples/manuscript-example.html#project-genomic-variant-to-a-new-transcript 
import hgvs.variantmapper as hgvs_biocommons_variantmapper 

# Importing https://github.com/counsyl/hgvs 
# How to setup data files : https://github.com/counsyl/hgvs/blob/master/examples/example1.py 
import pyhgvs as hgvs_counsyl
import pyhgvs.utils as hgvs_counsyl_utils
from pygr.seqdb import SequenceFileDB
# Use this package to retrieve genomic position for known refSeq entries.
# MutationInfo comes to the rescue when pyhgvs fails

from cruzdb import Genome as UCSC_genome # To Access UCSC https://pypi.python.org/pypi/cruzdb/ 

from pyVEP import VEP # Variant Effect Predictor https://github.com/kantale/pyVEP 

from bs4 import BeautifulSoup 

# For progress bar..
try:
	from IPython.core.display import clear_output
	have_ipython = True
except ImportError:
	have_ipython = False


__docformat__ = 'reStructuredText'
__version__ = '0.0.1'

"""
TODO: 
* More documentation   http://thomas-cokelaer.info/tutorials/sphinx/docstring_python.html 
* Fix setup.py http://stackoverflow.com/questions/3472430/how-can-i-make-setuptools-install-a-package-thats-not-on-pypi 
* Add hgvs_counsyl installation and automate these steps: https://github.com/counsyl/hgvs/blob/master/examples/example1.py  
	* Automate steps: Done
* Maybe import inline for performance reasons? http://stackoverflow.com/questions/477096/python-import-coding-style 

Notes:
* This link: http://www.ncbi.nlm.nih.gov/books/NBK21091/table/ch18.T.refseq_accession_numbers_and_mole/?report=objectonly
  Contains a list of all accession codes of NCBI  
* Interesting: M61857.1 Crashes mutalyzer.nl   
* None of the three methods of VariantMapper can convert from c. to g. 
	* http://hgvs.readthedocs.org/en/latest/modules/mapping.html#module-hgvs.variantmapper
* Clinvar : http://www.ncbi.nlm.nih.gov/clinvar/?term=M61857.1%3Ac.121A%3EG Could not identify variant M61857.1:c.121A>G 
* Interesting: NT_005120.15:g.-1126G>T is the same as NT_005120.15:g.1126G>T in mutalyzer 
	* https://mutalyzer.nl/name-checker?description=NT_005120.15%3Ag.-1126G%3ET 
"""

class MutationInfoException(Exception):
	pass

class MutationInfo(object):
	"""The MutationInfo class contains methods to get variant information 

	"""

	_properties_file = 'properties.json'
	biocommons_parser = hgvs_biocommons_parser.Parser() # https://bitbucket.org/biocommons/hgvs 

	# This is the size of the sequence, left and right to the variant 
	# position that we will attempt to perform a blat search on the
	# Reference genome
	# Accordint to this: https://genome.ucsc.edu/goldenPath/help/hgTracksHelp.html
	#    "DNA input sequences are limited to a maximum length of 25,000 bases"
	# Nevertheless the real maximum is 70.000 bases. In order not to "push"
	# UCSC's blat service we define a value of 2*20.000 = 40.0000
	# (20.000 to the left and 20.000 to the right)  
	blat_margin = 20000

	ucsc_blat_url = 'https://genome.ucsc.edu/cgi-bin/hgBlat'

	GrCh_genomes = {
		'hg18' : 'GrCh36',
		'hg19' : 'GRCh37',
		'hg38' : 'GRCh38',
	}

	# Link taken from: http://www.lovd.nl/3.0/docs/LOVD_manual_3.0.pdf page 71 
	lovd_genes_url = 'http://databases.lovd.nl/shared/api/rest.php/genes'
	lovd_variants_url = 'http://databases.lovd.nl/shared/api/rest.php/variants/{gene}'

	mutalyzer_url = 'https://mutalyzer.nl/name-checker?description={variant}'

	def __init__(self, local_directory=None, email=None, genome='hg19', dbsnp_version='snp142'):
		'''
		Current dbSNP version be default is 142 : 
			http://genome.ucsc.edu/goldenPath/newsarch.html
			11 February 2015 - dbSNP 142 Available for hg19 and hg38
		'''

		#Check genome value
		match = re.match(r'hg[\d]+', genome)
		if not match:
			raise ValueError('genome parameter should be hgDD (for example hg18, hg19, hg38, ...)')
		self.genome = genome
		if not self.genome in self.GrCh_genomes:
			raise KeyError('genome parameter: %s does not have an GrCh equivalent..' % (self.genome))
		self.genome_GrCh = self.GrCh_genomes[self.genome]

		#Do a simple check in dbsnp_version
		match = re.match(r'snp[\w]+', dbsnp_version)
		if not match:
			raise ValueError('dbsnp_version should be snpDDD (for example snp142)')
		self.dbsnp_version = dbsnp_version

		#Get local directory
		if local_directory is None:
			self.local_directory = Utils.get_application_dir('MutationInfo')
			logging.info('Using local directory: %s' % (self.local_directory)) 
		else:
			if not Utils.directory_exists(local_directory):
				raise EnvironmentError('Local Directory %s does not exist' % (str(local_directory)))
			self.local_directory = local_directory

		self._properties_file = os.path.join(self.local_directory, self._properties_file)
		if not Utils.file_exists(self._properties_file):
			#Create property file
			with open(self._properties_file, 'w') as f:
				f.write('{}\n')

		#Read property file
		self.properties = Utils.load_json_filename(self._properties_file)

		#Get email
		if not email is None:
			self.properties['email'] = email
		elif not 'email' in self.properties:
				self.properties['email'] = raw_input('I need an email to query Entrez. Please insert one: ')
		Entrez.email = self.properties['email']
		logging.info('Using email for accessing Entrez: %s' % (str(Entrez.email)))

		#Create transcripts directory
		self.transcripts_directory = os.path.join(self.local_directory, 'transcripts')
		logging.info('transcripts Directory: %s' % self.transcripts_directory)
		Utils.mkdir_p(self.transcripts_directory)

		#Create blat directory
		self.blat_directory = os.path.join(self.local_directory, 'blat')
		logging.info('blat Directory: %s' % (self.blat_directory))
		Utils.mkdir_p(self.blat_directory)

		self.counsyl_hgvs = Counsyl_HGVS(
			local_directory = self.local_directory,
			genome = self.genome,
			)

		logging.info('Connecting to biocommons uta..')
		self.biocommons_hdp = hgvs_biocommons_uta.connect()

		# http://hgvs.readthedocs.org/en/latest/examples/manuscript-example.html#project-genomic-variant-to-a-new-transcript 
		self.biocommons_vm_splign = hgvs_biocommons_variantmapper.EasyVariantMapper(self.biocommons_hdp, primary_assembly=self.genome_GrCh, alt_aln_method='splign')
		self.biocommons_vm_blat = hgvs_biocommons_variantmapper.EasyVariantMapper(self.biocommons_hdp, primary_assembly=self.genome_GrCh, alt_aln_method='blat')
		self.biocommons_vm_genewise = hgvs_biocommons_variantmapper.EasyVariantMapper(self.biocommons_hdp, primary_assembly=self.genome_GrCh, alt_aln_method='genewise')

		# Set up LOVD data 
		self._lovd_setup()

		# Set up mutalizer
		self.mutalyzer_directory = os.path.join(self.local_directory, 'mutalyzer')
		Utils.mkdir_p(self.mutalyzer_directory)
		logging.info('Mutalyzer directory: %s' % (self.mutalyzer_directory))

		# Set up cruzdb (UCSC)
		logging.info('Setting up UCSC access..')
		self.ucsc = UCSC_genome(self.genome)
		self.ucsc_dbsnp = getattr(self.ucsc, self.dbsnp_version)

		#Save properties file
		Utils.save_json_filenane(self._properties_file, self.properties)

	@staticmethod
	def biocommons_parse(variant):
		"""
		Parse a variant with the biocommons parser

		:param variant: The hgvs name of the variant
		"""
		try:
			return MutationInfo.biocommons_parser.parse_hgvs_variant(variant)
		except hgvs_biocommons.exceptions.HGVSParseError as e:
			logging.warning('Biocommons could not parse variant:  %s . Error: %s' % (str(variant), str(e)))
			return None

	@staticmethod
	def fuzzy_hgvs_corrector(variant, transcript=None, ref_type=None, **kwargs):
		"""
		Try to correct a wrong HGVS-ish variant by checking if it matches some patterns with common mistakes.
		Following directions from here: http://www.hgvs.org/mutnomen/recs-DNA.html#sub
		This is by far not exhaustive.. 

		:param variant: The name of the variant (example: 1234A>G)
		:param trascript: In case the variant does not have a transcript part then use this.
		:param ref_type: In case the variant does not include a reference type indicator (c or g) the define it here
		"""

		if ref_type not in [None, 'c', 'g']:
			raise ValueError('Available values for ref_type: None, "c" and "g" . Found: %s' % (str(ref_type)))

		#Exclude variants in unicode
		new_variant = str(variant)

		#Check if we have all necessary information
		if not ':' in new_variant:
			if transcript is None:
				logging.error('Variant: %s does not include a transcript part (":") and the transcript argument is None. Returning None ' % (new_variant))
				return None

			search = re.search(r'[cg]\.', new_variant)
			if search is None:
				if ref_type is None:
					logging.error('Variant: %s does not include a reference type part (c or g) and the ref_type argument is None. Returning None ' % (new_variant))
					return None
			new_variant = str(transcript) + ':' + ref_type + '.' + new_variant

		#Case 1
		#Instead if ">" the input is: "->". For example: 
		if '->' in new_variant:
			logging.warning('Variant: %s  . "->" found. Substituting it with ">"' % (new_variant))
			new_variant = new_variant.replace('->', '>')

		# Case 2
		# The variant contains / in order to declare two possible substitutions 
		search = re.search(r'([ACGT])>([ACGT])/([ACGT])', new_variant)
		if search:
			logging.warning('Variant: %s  . "/" found suggesting that it contains 2 variants' % (new_variant))
			new_variant_1 = re.sub(r'([ACGT])>([ACGT])/([ACGT])', r'\1>\2', new_variant)
			new_variant_2 = re.sub(r'([ACGT])>([ACGT])/([ACGT])', r'\1>\3', new_variant)
			return [
				MutationInfo.fuzzy_hgvs_corrector(new_variant_1), 
				MutationInfo.fuzzy_hgvs_corrector(new_variant_2)]

		# Case 3
		# -1126(C>T) 
		# The variant contains parenthesis in the substitition
		search = re.search(r'[\d]+\([ACGT]+>[ACGT]+\)', new_variant)
		if search:
			logging.warning('Variant: %s   . Contains parenthesis around substitition. Removing the parenthesis' % (new_variant))
			new_variant = re.sub(r'([\d]+)\(([ACGT]+)>([ACGT]+)\)', r'\1\2>\3', new_variant)

		#Case 4
		# NT_005120.15:c.1160CC>GT -->  NT_005120.15(UGT1A1):c.1160_1161delinsGT 
		search =re.search(r'([\-\d]+)([ACGT]+)>([ACGT]+)', new_variant)
		if search:
			if len(search.group(2)) > 1 or len(search.group(3)) > 1:
				logging.warning('Variant: %s   . Improper substitition please see: http://www.hgvs.org/mutnomen/recs-DNA.html#sub' % (new_variant))
				to_substitute = str(int(search.group(1))) + '_' + str(int(search.group(1)) + len(search.group(2)) -1 ) + 'delins' + search.group(3)
				new_variant = re.sub(r'([\-\d]+)([ACGT]+)>([ACGT]+)', to_substitute, new_variant)

		return new_variant

	def _get_info_rs(self, variant):
		return self._search_ucsc(variant)

	def _build_ret_dict(self, *args):
		return {
			'chrom' : args[0],
			'offset' : args[1],
			'ref' : args[2],
			'alt' : args[3],
			'genome' : args[4],
			'source' : args[5],
		}


	def get_info(self, variant, **kwargs):
		"""
		Doing our best to get the most out of a variant name

		:param variant: A variant or list of variants

		"""

		def get_elements_from_hgvs(hgvs):
			hgvs_transcript = hgvs.ac
			hgvs_type = hgvs.type
			hgvs_position = hgvs.posedit.pos.start.base
			hgvs_reference = hgvs.posedit.edit.ref
			if hasattr(hgvs.posedit.edit, 'alt'):
				hgvs_alternative = hgvs.posedit.edit.alt
			else:
				hgvs_alternative = None

			return hgvs_transcript, hgvs_type, hgvs_position, hgvs_reference, hgvs_alternative

		#Check the type of variant
		if type(variant) is list:
			ret = [self.get_info(v) for v in variant]
			return ret
		elif type(variant) is unicode:
			logging.info('Converting variant: %s from unicode to str and rerunning..' % (variant))
			ret = self.get_info(str(variant.strip()), **kwargs)
			return ret
		elif type(variant) is str:
			#This is expected
			pass
		else:
			logging.error('Unknown type of variant parameter: %s  (Accepted str and list)' % (type(variant).__name__))
			return None

		#Is this an rs variant?
		match = re.match(r'rs[\d]+', variant)
		if match:
			# This is an rs variant 
			logging.info('Variant %s is an rs variant. Looking at dbSNP..' % (variant))
			ret = self._get_info_rs(variant)
			if not ret:
				logging.warning('Variant: %s . UCSC Failed. Trying Variant Effect Predictor (VEP)' % (variant))
				return self._search_VEP(variant)

		#Is this an hgvs variant?
		hgvs = MutationInfo.biocommons_parse(variant)
		if hgvs is None:
			logging.warning('Variant: %s . Biocommons parsing failed. Trying to fix possible problems..' % (str(variant)))
			new_variant = MutationInfo.fuzzy_hgvs_corrector(variant, **kwargs)
			if type(new_variant) is list:
				return [self.get_info(v) for v in new_variant]
			elif type(new_variant) is str:
				hgvs = MutationInfo.biocommons_parse(new_variant)
				variant = new_variant

		if hgvs is None:
			#Parsing failed again.. 
			logging.warning('Biocommons failed to parse variant: %s .' % (variant))

			logging.info('Variant: %s . Trying to reparse with Mutalyzer and get the genomic description' % (variant))
			new_variant = self._search_mutalyzer(variant, **kwargs)
			if new_variant is None:
				logging.error('Variant: %s . Mutalyzer failed. Nothing left to do..' % (variant))
				return None
			logging.info('Variant: %s . rerunning get_info with variant=%s' % (variant, new_variant))
			return self.get_info(new_variant, **kwargs)

		#Up to here we have managed to parse the variant
		hgvs_transcript, hgvs_type, hgvs_position, hgvs_reference, hgvs_alternative = get_elements_from_hgvs(hgvs)


		#Try to map the variant in the reference assembly with biocommons
		if hgvs_type == 'c':
			logging.info('Variant: %s . Trying to map variant in the reference assembly with biocommons' % (variant))
			success = False

			for biocommons_vm_name, biocommons_vm_method in [
					('splign', self.biocommons_vm_splign), 
					('blat', self.biocommons_vm_blat), 
					('genewise', self.biocommons_vm_genewise),
				]:

				try:
					logging.info('Trying biocommon method: %s' % (biocommons_vm_name))
					hgvs_reference_assembly = biocommons_vm_method.c_to_g(hgvs)
					hgvs_transcript, hgvs_type, hgvs_position, hgvs_reference, hgvs_alternative = get_elements_from_hgvs(hgvs_reference_assembly)
					success = True
				except hgvs_biocommons.exceptions.HGVSDataNotAvailableError as e:
					logging.warning('Variant: %s . %s method failed: %s' % (variant, biocommons_vm_name, str(e)))
				except hgvs_biocommons.exceptions.HGVSError as e:
					logging.error('Variant: %s . biocommons reported error: %s' % (variant, str(e)))

				if success:
					break

		#Is this a reference assembly?
		if self._get_ncbi_accession_type(hgvs_transcript) == 'NC':
			logging.info('Variant: %s . is a Complete genomic molecule, reference assembly' % (variant))
			#ncbi_info = self._get_info_from_nucleotide_entrez(hgvs_transcript, retmode='text', rettype='asn.1')
			ncbi_info = self._get_data_from_nucleotide_entrez(hgvs_transcript, retmode='text', rettype='asn.1')
			search = re.search(r'Homo sapiens chromosome ([\w]+), ([\w\.]+) Primary Assembly', ncbi_info)
			if search is None:
				logging.error('Variant: %s . Although this variant is a reference assembly, could not locate the chromosome and assembly name in the NCBI entry' % (variant))
				return None
			ret = self._build_ret_dict(search.group(1), hgvs_position, hgvs_reference, hgvs_alternative, search.group(2), 'NC_transcript')
			return ret

		logging.info('Biocommons Failed')

		logging.info('Variant: %s Converting to VCF with pyhgvs..' % (variant)) 
		try:
			chrom, offset, ref, alt = self.counsyl_hgvs.hgvs_to_vcf(variant)
			return self._build_ret_dict(chrom, offset, ref, alt, self.genome, 'counsyl_hgvs_to_vcf')
		except KeyError as e:
			logging.warning('Variant: %s . pyhgvs KeyError: %s' % (variant, str(e)))
		except ValueError as e:
			logging.warning('Variant: %s . pyhgvs ValueError: %s' % (variant, str(e)))
		except IndexError as e:
			logging.warning('Variant: %s . pyhgvs IndexError: %s' % (variant, str(e)))

		logging.info('counsyl pyhgvs failed...')

		logging.info('Trying LOVD..')
		lovd_chrom, lovd_pos_1, lovd_pos_2, lovd_genome = self._search_lovd(hgvs_transcript, 'c.' + str(hgvs.posedit))
		if not lovd_chrom is None:
			logging.warning('***SERIOUS*** strand of variant has not been checked!')
			return self._build_ret_dict(lovd_chrom, lovd_pos_1, hgvs_reference, hgvs_alternative, lovd_genome, 'LOVD')

		logging.info('LOVD failed..')

		logging.info('Fetching fasta sequence for trascript: %s' % (hgvs_transcript))
		#fasta = self._get_fasta_from_nucleotide_entrez(hgvs_transcript)
		fasta = self._get_data_from_nucleotide_entrez(hgvs_transcript, retmode='text', rettype='fasta')

		# Check variant type
		if hgvs_type == 'c':
			logging.warning('Variant: %s . This is a c (coding DNA) variant. Trying to infer g position..' % (variant))
			#logging.info('Variant: %s . Fetching NCBI XML for transcript: %s' % (variant, hgvs_transcript))
			logging.info('Variant: %s . Fetching genbank entry for transcript: %s' % (variant, hgvs_transcript))
			#ncbi_xml = self._get_xml_from_nucleotide_entrez(hgvs_transcript)
			#ncbi_xml = self._get_data_from_nucleotide_entrez(hgvs_transcript, retmode='text', rettype='xml')
			#genbank = self._get_data_from_nucleotide_entrez(hgvs_transcript, retmode='text', rettype='gb')
			genbank = self._get_data_from_nucleotide_entrez(hgvs_transcript, retmode='text', rettype='gbwithparts')
			genbank_filename = self._ncbi_filename(hgvs_transcript, 'gbwithparts')
			logging.info('Variant: %s . Genbank filename: %s' % (variant, genbank_filename))
			if 'gene' in kwargs:
				genbank_gene = kwargs['gene']
			else:
				genbank_gene = None
			genbank_c_to_g_mapper = self._get_sequence_features_from_genbank(genbank_filename, gene=genbank_gene)
			new_hgvs_position = genbank_c_to_g_mapper(int(hgvs_position))
			logging.info('Variant: %s . New hgvs g. position: %i   Old c. position: %i' % (variant, new_hgvs_position, hgvs_position))
			hgvs_position = new_hgvs_position
			hgvs_type = 'g'
			
		elif hgvs_type == 'g':
			#This should be fine
			pass
		else:
			logging.error('Variant: %s Sorry.. only c (coding DNA) and g (genomic) variants are supported so far.' % (variant))
			return None

		logging.info('Variant: %s . Reference on fasta: %s  Reference on variant: %s' % (variant, fasta[hgvs_position-1], hgvs_reference))
		if fasta[hgvs_position-1] != hgvs_reference:
			logging.error('Variant: %s . ***SERIOUS*** Reference on fasta (%s) and Reference on variant name (%s) are different!' % (variant, fasta[hgvs_position-1], hgvs_reference))

		logging.info('Variant: %s . Fasta length: %i' % (variant, len(fasta)))
		logging.info('Variant: %s . Variant position: %i' % (variant, hgvs_position))

		#relatve_pos is the relative position in the 2*blat_margin sample of the variant
		relative_pos = hgvs_position

		#Take an as much as possible chunk of the fasta
		if hgvs_position - self.blat_margin < 0:
			chunk_start = 0
		else:
			chunk_start = hgvs_position - self.blat_margin
			relative_pos = self.blat_margin

		if hgvs_position + self.blat_margin > len(fasta):
			chunk_end = len(fasta)
		else:
			chunk_end = hgvs_position + self.blat_margin

		fasta_chunk = fasta[chunk_start:chunk_end]
		logging.info('Variant: %s . Chunk position [start, end] = [%i, %i]' % (variant, chunk_start, chunk_end))
		logging.info('Variant: %s . Position of variant in chunk: %i ' % (variant, relative_pos))
		logging.info('Variant: %s . Reference on chunk: %s   Reference on fasta: %s  Reference at variant position +/- 1: %s' % (variant, fasta_chunk[relative_pos-1], fasta[hgvs_position-1], fasta_chunk[relative_pos-2:relative_pos+1]))

		assert fasta_chunk[relative_pos-1] == fasta[hgvs_position-1]

		#Now that we have a fair sample of the sample 
		# We can blat it!
		blat_filename = self._create_blat_filename(hgvs_transcript, chunk_start, chunk_end)
		logging.info('Variant: %s . Blat results filename: %s' % (variant, blat_filename) )
		if not Utils.file_exists(blat_filename):
			logging.info('Variant: %s . Blat filename does not exist. Requesting it from UCSC..' % (variant) )
			self._perform_blat(fasta_chunk, blat_filename)

		logging.info('Variant: %s . Blat filename exists (or created). Parsing it..' % (variant))
		blat = self._parse_blat_results_filename(blat_filename)

		#Log some details regarding the blat results
		logging.info('Variant: %s . Blat identity: %s' % (variant, blat[0][u'IDENTITY']))
		logging.info('Variant: %s . Blat Span: %s' % (variant, blat[0][u'SPAN']))
		chrom = blat[0][u'CHRO']
		logging.info('Variant: %s . Chromosome: %s' % (variant, chrom))
		blat_details_url = blat[0]['details_url']
		logging.info('Variant: %s . Details URL: %s' % (variant, blat_details_url))
		blat_alignment_filename = self._create_blat_alignment_filename(hgvs_transcript, chunk_start, chunk_end)
		logging.info('Variant: %s . Blat alignment filename: %s' % (variant, blat_alignment_filename))

		if not Utils.file_exists(blat_alignment_filename):
			logging.info('Variant: %s . Blat alignment filename does not exist. Creating it..' % (variant))
			blat_temp_alignment_filename = blat_alignment_filename + '.tmp'
			logging.info('Variant: %s . Temporary blat alignment filename: %s' % (variant, blat_temp_alignment_filename))
			logging.info('Variant: %s . Downloading Details url in Temporary blat alignment filename' % (variant))
			Utils.download(blat_details_url, blat_temp_alignment_filename)
			logging.info('Variant: %s . Parsing temporary blat alignment filename' % (variant))
			with open(blat_temp_alignment_filename) as blat_temp_alignment_file:
				blat_temp_alignment_soup =  BeautifulSoup(blat_temp_alignment_file)
			blat_real_alignment_url = 'https://genome.ucsc.edu/' + blat_temp_alignment_soup.find_all('frame')[1]['src'].replace('../', '')
			logging.info('Variant: %s . Real blat alignment URL: %s' % (variant, blat_real_alignment_url))
			blat_real_alignment_filename = blat_alignment_filename + '.html'
			logging.info('Variant: %s . Real blat alignment filename: %s' % (variant, blat_real_alignment_url))
			logging.info('Variant: %s . Downloading real blat alignment filename..' % (variant))
			Utils.download(blat_real_alignment_url, blat_real_alignment_filename)
			logging.info('Variant: %s . Reading content from real alignment filename' % (variant))
			with open(blat_real_alignment_filename) as blat_real_alignment_file:
				# We have to set html.parser otherwise parsing is incomplete
				blat_real_alignment_soup = BeautifulSoup(blat_real_alignment_file, 'html.parser')
				#Take the complete text
				blat_real_alignment_text = blat_real_alignment_soup.text

			logging.info('Variant: %s . Saving content to blat alignment filename: %s' % (variant, blat_alignment_filename))
			with open(blat_alignment_filename, 'w') as blat_alignment_file:
				blat_alignment_file.write(blat_real_alignment_text)

		logging.info('Variant: %s . Blat alignment filename exists (or created)' % (variant))
		human_genome_position, direction = self._find_alignment_position_in_blat_result(blat_alignment_filename, relative_pos, verbose=True)
		logging.info('Variant: %s . Blat alignment position: %i, direction: %s' % (variant, human_genome_position, direction))

		#Invert reference / alternative if sequence was located in negative strand 
		if direction == '-':
			# TODO : Reverse also sequence for deletions / additions 
			hgvs_reference = self.inverse(hgvs_reference)
			hgvs_alternative = self.inverse(hgvs_alternative)

		ret = self._build_ret_dict(chrom, human_genome_position, hgvs_reference, hgvs_alternative, self.genome, 'BLAT')
		return ret

	@staticmethod
	def inverse(nucleotide):
		inverter = {
			'A' : 'T',
			'T' : 'A',
			'C' : 'G',
			'G' : 'C',
		}

		if nucleotide is None:
			return None

		return ''.join([inverter[x] for x in nucleotide.upper()])

	def _create_blat_filename(self, transcript, chunk_start, chunk_end):
		return os.path.join(self.blat_directory, 
			transcript + '_' + str(chunk_start) + '_' + str(chunk_end) + '.blat.results.html')

	def _create_blat_alignment_filename(self, transcript, chunk_start, chunk_end):
		return os.path.join(self.blat_directory, 
			transcript + '_' + str(chunk_start) + '_' + str(chunk_end) + '.blat')

	def _entrez_request(self, ncbi_access_id, retmode, rettype):
		'''
		http://www.ncbi.nlm.nih.gov/books/NBK25499/table/chapter4.T._valid_values_of__retmode_and/?report=objectonly 
		'''
		handle = Entrez.efetch(db='nuccore', id=ncbi_access_id, retmode=retmode, rettype=rettype)
		data = handle.read()
		handle.close()

		return data

	def _get_data_from_nucleotide_entrez(self, ncbi_access_id, retmode, rettype):

		filename = self._ncbi_filename(ncbi_access_id, rettype)
		logging.info('NCBI %s %s filename: %s' % (retmode, rettype, filename))

		if Utils.file_exists(filename):
			logging.info('Filename: %s exists.' % (filename))
			data = self._load_ncbi_filename(ncbi_access_id, rettype)
		else:
			logging.info('Filename: %s does not exist. Querying ncbi through Entrez..' % (filename))
			data = self._entrez_request(ncbi_access_id, retmode, rettype)
			self._save_ncbi_filename(ncbi_access_id, rettype, data)
			logging.info('Filename: %s created.' % (filename))

		if rettype == 'fasta':
			return self.strip_fasta(data)
		else:
			return data

	@staticmethod
	def strip_fasta(fasta):
		'''
		Strips comments and newline characters from fasta data
		'''
		return ''.join([x for x in fasta.split('\n') if '>' not in x])


	def _ncbi_filename(self, ncbi_access_id, rettype):
		'''
		Create filename that contains NCBI fasta file
		rettype : fasta , xml , gb (genbank)
		'''
		return os.path.join(self.transcripts_directory, ncbi_access_id + '.' + rettype)


	def _save_ncbi_filename(self, ncbi_access_id, rettype, data):
		'''
		Save NCBI fasta to file
		'''

		filename = self._ncbi_filename(ncbi_access_id, rettype)
		with open(filename, 'w') as f:
			f.write(data)

	def _load_ncbi_filename(self, ncbi_access_id, rettype):
		'''
		Load NCBI fasta file
		'''
		filename = self._ncbi_filename(ncbi_access_id, rettype)

		with open(filename) as f:
			data = f.read()

		return data


	def _perform_blat(self, fasta, output_filename):
		'''
		Perform a blat request at UCSC 
		Saves results in output_filename

		TODO:
		* Support organisms other than Human
		* Error check on request.post 
		'''

		data = {
			'org':'Human', 
			'db':self.genome, 
			'sort':'query,score', 
			'output':'hyperlink', 
			'userSeq': fasta, 
			'type':"BLAT's guess"
		}

		logging.info('Requesting data from UCSC\'s blat..')
		r = requests.post(self.ucsc_blat_url, data=data)
		logging.info('   ... Request is done')

		with open(output_filename, 'w') as f:
			f.write(r.text)

		return True

	@staticmethod
	def _parse_blat_results_filename(input_filename):
		'''
		Parse the html blat results filename 

		TODO: 
		* Improve readability..
		'''

		with open(input_filename) as f:
			soup = BeautifulSoup(f, 'html.parser')

		header = soup.find_all('pre')[0].text.split('\n')[0].split()[1:]
		header[header.index('START')] = 'RELATIVE_START'
		header[header.index('END')] = 'RELATIVE_END'

		all_urls = [x.get('href') for x in soup.find_all('pre')[0].find_all('a')]
		all_urls_pairs = zip(all_urls[::2], all_urls[1::2])

		ret = [{k:v for k,v in zip(header, x.split()[2:])} for x in soup.find_all('pre')[0].text.split('\n') if 'details' in x]

		for i, x in enumerate(ret):
			ret[i]['browse_url'] = 'https://genome.ucsc.edu/cgi-bin' + all_urls_pairs[i][0].replace('../cgi-bin', '')
			ret[i]['details_url'] = 'https://genome.ucsc.edu/cgi-bin' + all_urls_pairs[i][1].replace('../cgi-bin', '')

		return ret

	@staticmethod
	def _find_alignment_position_in_blat_result(blat_filename, pos, verbose=True):

		print 'Position:', pos

		def get_pos(record, index):
			#print record
			return int(re.findall(r'[\d]+', record)[index])

		def get_sequence(record, index):
			return re.findall(r'[acgt\.]+', record)[index]

		def get_matching(record):
			match = re.search(r'[\<\>]+ ([\|\ ]*) [\<\>]+', record)
			return match.group(1)

		with open(blat_filename) as f:
			blat_results = f.read()

		blat_records = re.findall(r'[\d]* [acgt\.]* [\d]*\n[\<\>]+ [\|\ ]* [\<\>]+\n[\d]* [acgt\.]* [\d]*', blat_results)

		for blat_index, blat_record in enumerate(blat_records):
			fasta_start = get_pos(blat_record, 0)
			fasta_end = get_pos(blat_record, 1)

			if fasta_start <= pos <= fasta_end:
				break

		if verbose:
			print blat_record

		fasta_sequence = get_sequence(blat_record, 0)
		reference_sequence = get_sequence(blat_record, 1)
		alignment_start = get_pos(blat_record, 2)
		alignment_end = get_pos(blat_record, 3)

		if alignment_start < alignment_end:
			alignment_step = 1
			direction = '+'
		elif alignment_start > alignment_end:
			alignment_step = -1
			direction = '-'
		else:
			raise Exception('WTF!')

		matching = get_matching(blat_record)

		#Find position in fasta sequence
		#fasta_real_index = fasta_start
		fasta_real_index = None
		for fasta_absolute_index, (fasta_index, alignment_index) in enumerate(zip(range(fasta_start, fasta_end+1), range(alignment_start, alignment_end+alignment_step, alignment_step))):

			if fasta_sequence[fasta_absolute_index] != '.':
				if fasta_real_index is None:
					fasta_real_index = fasta_index
				else:
					fasta_real_index += 1

			last_sequence = fasta_sequence[fasta_absolute_index]
			if verbose:
				print 'Seq: %s   Alignment: %i   Fasta: %i   Real_fasta: %i   Match: %s' % (fasta_sequence[fasta_absolute_index], alignment_index, fasta_index, fasta_real_index, matching[fasta_absolute_index])

			if fasta_real_index == pos:
				break

		#assert fasta_real_index != fasta_start
		if matching[fasta_absolute_index] != '|':
			print '***** WARNING: This position does not have a match with aligned sequence'
			#raise Exception('This position does not have a match with aligned sequence')

		print last_sequence
		return alignment_index, direction


	@staticmethod
	def _get_ncbi_accession_type(transcript):
		'''

		Get the accession type of a transcript

		List of all accessions
		http://www.ncbi.nlm.nih.gov/books/NBK21091/table/ch18.T.refseq_accession_numbers_and_mole/?report=objectonly
		'''

		# Headers 
		# Accession prefix	Molecule type	Comment
		accession_types = {
			'AC_': ['Genomic', 'Complete genomic molecule, usually alternate assembly'],
			'NC_': ['Genomic', 'Complete genomic molecule, usually reference assembly'],
			'NG_': ['Genomic', 'Incomplete genomic region'],
			'NT_': ['Genomic', 'Contig or scaffold, clone-based or WGSa'],
			'NW_': ['Genomic', 'Contig or scaffold, primarily WGSa'],
			'NS_': ['Genomic', 'Environmental sequence'],
			'NZ_': ['Genomic', 'Unfinished WGS'],
			'NM_': ['mRNA', ''],
			'NR_': ['RNA', ''],
			'XM_': ['mRNA', 'Predicted model'],
			'XR_': ['RNA', 'Predicted model'],
			'AP_': ['Protein', 'Annotated on AC_ alternate assembly'],
			'NP_': ['Protein', 'Associated with an NM_ or NC_ accession'],
			'YP_': ['Protein', ''],
			'XP_': ['Protein', 'Predicted model, associated with an XM_ accession'],
			'ZP_': ['Protein', 'Predicted model, annotated on NZ_ genomic records'],
		}

		search = re.search(r'^\w\w_', transcript)
		if search is None:
			logging.warning('Transcript: %s does not follow a WW_ pattern' % (transcript))
			return None

		if not search.group() in accession_types:
			logging.warning('Accesion type: %s of transcript: %s does not belong to known accesion types' % (search.group(), transcript))
			return None

		ret = search.group()[0:-1] # Remove '_'
		return ret

	@staticmethod
	def _get_sequence_features_from_genbank(filename, gene=None):

		def get_feature_genes(features):
			feature_genes = [feature.qualifiers['gene'] for feature in features]
			feature_genes_flat_set = list(set([y for x in feature_genes for y in x]))

			return feature_genes_flat_set

		def select_gene(features, genes):
			if len(genes) == 1:
				logging.info('Genbank filename: %s . Selecting unique gene: %s' % (filename, genes[0]))
				selected_gene = genes[0]
			else:
				if gene is None:
					logging.error('Genbank filename: %s . gene parameter is None . please select one of the following genes: %s' % (filename, genes))
					return None
				else:
					if gene in genes:
						selected_gene = gene
					else:
						logging.error('Genbank filename: %s . gene: %s is not present in genbank file' % (filename, gene))

			return [feature for feature in features if selected_gene in feature.qualifiers['gene']]

		def get_positions(features):
			ret = []
			assert len(features) == 1
			feature = features[0]

			if len(feature.sub_features) == 0:
				ret.append((
					feature.location.start.position, 
					feature.location.end.position, 
					feature.location.strand
					))
			else:
				for sub_feature in feature.sub_features:
					ret.append((
						sub_feature.location.start.position, 
						sub_feature.location.end.position, 
						sub_feature.location.strand
						))

			return ret

		def select_features(record, f_type):
			all_f = [x for x in record.features if x.type == f_type]
			if len(all_f) == 0:
				return []

			all_f_genes = get_feature_genes(all_f)
			all_f_gene_features = select_gene(all_f, all_f_genes)
			if all_f_gene_features is None:
				return None

			all_f_positions = get_positions(all_f_gene_features)
			return all_f_positions

		def make_ret_function(CDS):
			start = CDS[0][0] + 1
			end = CDS[-1][1]


			def ret_f(c_pos):

				if c_pos < 1:
					return start + c_pos

				previous_dif = 0
				for CDS_position in CDS:
					current_dif = CDS_position[1] - CDS_position[0]
					if previous_dif <= c_pos <= previous_dif + current_dif:
						return CDS_position[0] + c_pos - previous_dif 
					previous_dif += current_dif
				return CDS[-1][1] + c_pos - previous_dif 

			return ret_f

		with open(filename) as f:
			records = list(SeqIO.parse(f, 'genbank'))

		if len(records) != 1:
			logging.error('Genbank file: %s . Found more than one genbank record.' % (filename))
			return None

		#Get CDS 
		CDS_positions = select_features(records[0], 'CDS')
		mRNA_positions = select_features(records[0], 'mRNA')

		if not CDS_positions is None and len(CDS_positions) == 0:
			logging.warning('Genbank file %s . No CDS features found.' % (filename))

		print 'CDS:', CDS_positions
		print 'mRNA:', mRNA_positions

		if CDS_positions:
			return make_ret_function(CDS_positions)

		return make_ret_function(mRNA_positions)

	@staticmethod
	def _get_sequence_features_from_XML_NCBI(filename):
		'''
		DEPRECATED!! 
		Use _get_sequence_features_from_genbank instead

		Return all sequence features from XML NCBI file.
		returns a dictionary with start and end positions of each feature

	Data for debugging:
	# record[u'Bioseq-set_seq-set'][0][u'Seq-entry_seq'][u'Bioseq'][u'Bioseq_annot'][0][u'Seq-annot_data'][u'Seq-annot_data_ftable'][i][u'Seq-feat_data'][u'SeqFeatData'][u'SeqFeatData_rna'][u'RNA-ref'][u'RNA-ref_type'].attributes[u'value']
	# record[u'Bioseq-set_seq-set'][0][u'Seq-entry_seq'][u'Bioseq'][u'Bioseq_annot'][0][u'Seq-annot_data'][u'Seq-annot_data_ftable'][i][u'Seq-feat_location'][u'Seq-loc'][u'Seq-loc_int'][u'Seq-interval'][u'Seq-interval_from']
	# record[u'Bioseq-set_seq-set'][0][u'Seq-entry_seq'][u'Bioseq'][u'Bioseq_annot'][0][u'Seq-annot_data'][u'Seq-annot_data_ftable'][i][u'Seq-feat_location'][u'Seq-loc'][u'Seq-loc_int'][u'Seq-interval'][u'Seq-interval_to']

	# record[u'Bioseq-set_seq-set'][0][u'Seq-entry_seq'][u'Bioseq'][u'Bioseq_annot'][0][u'Seq-annot_data'][u'Seq-annot_data_ftable'][1][u'Seq-feat_location'][u'Seq-loc'].keys() --> [u'Seq-loc_pnt']

	# record[u'Bioseq-set_seq-set'][0][u'Seq-entry_seq'][u'Bioseq'][u'Bioseq_annot'][0][u'Seq-annot_data'][u'Seq-annot_data_ftable'][3][u'Seq-feat_data'][u'SeqFeatData'].keys() --> [u'SeqFeatData_gene'] 
	# record[u'Bioseq-set_seq-set'][0][u'Seq-entry_seq'][u'Bioseq'][u'Bioseq_annot'][0][u'Seq-annot_data'][u'Seq-annot_data_ftable'][3][u'Seq-feat_data'][u'SeqFeatData'][u'SeqFeatData_gene'][u'Gene-ref'][u'Gene-ref_locus'] --> CYP2C9 

	# ---------------------------------------- 
	# record[u'Bioseq-set_seq-set'][0][u'Seq-entry_set'][u'Bioseq-set'][u'Bioseq-set_annot'][0][u'Seq-annot_data'][u'Seq-annot_data_ftable'][0][u'Seq-feat_location']

	# ----------------------------------------
	# record[u'Bioseq-set_seq-set'][0][u'Seq-entry_seq'][u'Bioseq'][u'Bioseq_annot'][0][u'Seq-annot_data'][u'Seq-annot_data_ftable'][0][u'Seq-feat_location'][u'Seq-loc'][u'Seq-loc_mix'][u'Seq-loc-mix'][10]


	Test with
	#Check XML parsers
	hgvs_transcripts = ['NM_052896.3', 'M61857.1']
	for hgvs_transcript in hgvs_transcripts: 
		ncbi_xml = mi._get_data_from_nucleotide_entrez(hgvs_transcript, retmode='text', rettype='xml')
		ncbi_xml_filename = mi._ncbi_filename(hgvs_transcript, 'xml')
		print 'Filename: ', ncbi_xml_filename
		ncbi_xml_features = mi._get_sequence_features_from_XML_NCBI(ncbi_xml_filename)
		print 'Features:', ncbi_xml_features


		'''

		fields_1 = [
			u'Bioseq-set_seq-set',
			0,
			(u'Seq-entry_seq', u'Seq-entry_set'),
			(u'Bioseq', u'Bioseq-set'),
			(u'Bioseq_annot', u'Bioseq-set_annot'),
			0,
			u'Seq-annot_data',
			u'Seq-annot_data_ftable',
		]

		def apply_field(record, fields, starting_path):
			current_record = record
			current_path = starting_path

			for field in fields:

				if type(field) is tuple:
					found = False
					for tuple_field in field:
						if tuple_field in current_record:
							current_field = tuple_field
							found = True
							break
					if not found:
						logging.error('Could not find record: %s in path: %s in XML Entrez filename: %s' % (str(field), current_path, filename))
						return None
				else:
					current_field = field

				current_path += u' --> ' + str(current_field)
				#print current_path
				if type(current_record).__name__ == 'DictionaryElement':
					if current_field in current_record:
						current_record = current_record[current_field]
					else:
						logging.error('Could not find record: %s in XML Entrez filename: %s' % (current_path, filename))
						logging.error('Available keys: %s' % (str(current_record.keys())))
						return None
				elif type(current_record).__name__ == 'ListElement':
					if len(current_record) < current_field:
						logging.error('Could not find record: %s in XML Entrex filename: %s' % (current_path, filename))
						return None
					else:
						current_record = current_record[current_field]
				else:
					raise Exception('Unknown XML field type: %s' % type(current_record).__name__)

			return current_record, current_path


		with open(filename) as f:
			record = Entrez.read(f)

		path = 'START'

		current_record, path = apply_field(record, fields_1, path)

		ret = {}

		# Keep only data entries of the feature table
		for location_entry_index, location_entry in enumerate(current_record):
			current_path = path + u' --> ' + str(location_entry_index)
			loc_current_entry, loc_path = apply_field(location_entry, [u'Seq-feat_data', u'SeqFeatData'], current_path)
			
			location_keys = loc_current_entry.keys()
			if len(location_keys) != 1:
				logging.error('Entrez XML file: %s , path: %s has more than one record: %s' % (filename, loc_path, str(location_keys)))
				return None

			location_key = location_keys[0]
			search = re.search(r'_([\w]+)$', location_key)
			if search is None:
				logging.error('Entrez XML file: %s , path: %s . Cannot process key: %s (expected YYY_ZZZ name (for example: SeqFeatData_rna))' % (filename, loc_path, location_key))
				return None

			key = search.group(1)
			loc_current_entry, loc_path = apply_field(loc_current_entry, ['SeqFeatData_%s' % (key)], loc_path)
		
			ref_keys = loc_current_entry.keys()
			if len(ref_keys) != 1:
				logging.error('Entrez XML file: %s , path: %s has more than one record: %s' % (filename, loc_path, str(ref_keys)))
				return None

			ref_key = ref_keys[0]
			if not ref_key in [u'Cdregion']:
				search = re.search(r'([\w]+)-([\w]+)', ref_key)
				if search is None:
					logging.error('Entrex XML file: %s , path: %s . Cannot process key: %s (expected YYY-ZZZ name (for example: Gene-ref))' % (filename, loc_path, ref_key))
					return None

				if search.group(2) != u'ref':
					#Ignore these entries
					continue

				if search.group(1).lower() != key.lower():
					logging.error('Entrez XML file: %s , path: %s . Could not find expected key: %s' % (filename, loc_path, key + '-ref'))
					return None

				loc_current_entry, loc_path = apply_field(loc_current_entry, [ref_key], loc_path)
				#We do not traverse any further. We keep the key as the element name
				#We continue to seek the interval positions


#			loc_current_entry, loc_path = apply_field(location_entry, [u'Seq-feat_location', u'Seq-loc', u'Seq-loc_int', u'Seq-interval', u'Seq-interval_from'], current_path)
			loc_current_entry, loc_path = apply_field(location_entry, [u'Seq-feat_location', u'Seq-loc'], current_path)
			if u'Seq-loc_int' in loc_current_entry:
				loc_current_entry, loc_path = apply_field(loc_current_entry, [u'Seq-loc_int', u'Seq-interval'], loc_path)
				sequence_from, _ = apply_field(loc_current_entry, [u'Seq-interval_from'], loc_path)
				sequence_to, _ = apply_field(loc_current_entry, [u'Seq-interval_to'], loc_path)
				ret[key] = [sequence_from, sequence_to]
			elif u'Seq-loc_mix' in loc_current_entry:
				loc_current_entry, loc_path = apply_field(loc_current_entry, [u'Seq-loc_mix', u'Seq-loc-mix'], loc_path)
				ret[key] = []
				for seq_loc_mix_index, seq_loc_mix in enumerate(loc_current_entry):
					seq_loc_path = loc_path + ' --> %i ' % (seq_loc_mix_index)
					loc_current_entry, loc_path = apply_field(seq_loc_mix, [u'Seq-loc_int', u'Seq-interval'], seq_loc_path)
					sequence_from, _ = apply_field(loc_current_entry, [u'Seq-interval_from'], loc_path)
					sequence_to, _ = apply_field(loc_current_entry, [u'Seq-interval_to'], loc_path)
					ret[key].append([sequence_from, sequence_to])
			elif u'Seq-loc_packed-int' in loc_current_entry:
				loc_current_entry, loc_path = apply_field(loc_current_entry, [u'Seq-loc_packed-int', u'Packed-seqint'], loc_path)
				ret[key] = []
				for seq_loc_mix_index, seq_loc_mix in enumerate(loc_current_entry):
					seq_loc_path = loc_path + ' --> %i ' % (seq_loc_mix_index)
					#loc_current_entry, loc_path = apply_field(seq_loc_mix, [u'Seq-loc_int', u'Seq-interval'], seq_loc_path)
					sequence_from, _ = apply_field(seq_loc_mix, [u'Seq-interval_from'], seq_loc_path)
					sequence_to, _ = apply_field(seq_loc_mix, [u'Seq-interval_to'], seq_loc_path)
					ret[key].append([sequence_from, sequence_to])
			else:
				if hasattr(loc_current_entry, 'keys'):
					logging.error('Entrez XML file: %s , path: %s . Could not find Seq-loc_int OR Seq-loc_mix . Existing keys: %s' % (filename, loc_path, str(loc_current_entry.keys())))
					return None
				else:
					a=1/0

#			sequence_from = loc_current_entry

#			loc_current_entry, loc_path = apply_field(location_entry, [u'Seq-feat_location', u'Seq-loc', u'Seq-loc_int', u'Seq-interval', u'Seq-interval_to'], current_path)
#			sequence_to = loc_current_entry

#			ret[key] = [sequence_from, sequence_to]

		return ret

	def _lovd_setup(self):

		self.lovd_directory = os.path.join(self.local_directory, 'LOVD')
		logging.info('LOVD directory: %s' % (self.lovd_directory))
		Utils.mkdir_p(self.lovd_directory)

		self.lovd_genes_atom = os.path.join(self.lovd_directory, 'genes.atom')
		logging.info('LOVD genes atom filename: %s' % (self.lovd_genes_atom))

		#Check if genes_atom file exists
		if not Utils.file_exists(self.lovd_genes_atom):
			logging.info('File %s does not exist. Downloading from: %s' % (self.lovd_genes_atom, self.lovd_genes_url))
			Utils.download(self.lovd_genes_url, self.lovd_genes_atom)

		self.lovd_genes_json = os.path.join(self.lovd_directory, 'genes.json')
		logging.info('LOVD gene json filename: %s' % (self.lovd_genes_json))
		#Check if it exists
		if Utils.file_exists(self.lovd_genes_json):
			logging.info('LOVD gene json filename %s exists. Loading..' % (self.lovd_genes_json))
			self.lovd_transcript_dict = Utils.load_json_filename(self.lovd_genes_json)
			return

		logging.info('LOVD gene json filename does not exist. Creating it..')

		logging.info('Parsing LOVD genes file: %s ..' % (self.lovd_genes_atom))
		data = feedparser.parse(self.lovd_genes_atom)

		logging.info('Parsed LOVD genes file with %s entries' % (len(data['entries'])))

		ret = {}
		for entry_index, entry in enumerate(data['entries']):
			summary = entry['summary']
			
		#	if entry_index % 100 == 0:
		#		logging.info('Parsed entries: %i', entry_index)

			# id:A1BG
			search = re.search(r'id:([\w]+)', summary) 
			if search is None:
				message = 'Could not find ID in LOVD entry: %s' % (summary)
				logging.error(message)
				#This shouldn't happen..
				raise MutationInfoException(message)
			_id = search.group(1)

			# refseq_build:hg19
			search = re.search(r'refseq_build:([\w]+)', summary)
			if search is None:
				message = 'Could not find refseq_build in LOVD entry: %s' % (summary)
				logging.error(message)
				refseq_build = None
			else:
				refseq_build = search.group(1)


			# refseq_mrna:NM_130786.3 
			search = re.search(r'refseq_mrna:([\w_\.]+)', summary)
			if search is None:
				refseq_mrna = None
				#message = 'Could not find refseq_mrna on LOVD entry: %s ' % (summary)
				#logging.warning(message)
				# This shouldn't happen
				#raise MutationInfoException(message)
			else:
				refseq_mrna = search.group(1)
				if refseq_mrna in ret:
					message = 'mRNA Refseq entry %s is appeared in more than one genes: %s, %s' % (refseq_mrna, ret[refseq_mrna], _id)
					logging.error(message)
					raise MutationInfoException('Entr')
				ret[refseq_mrna] = [_id, refseq_build]

		self.lovd_transcript_dict = ret
		logging.info('Built LOVD trascript dictionary')

		logging.info('Saving to json file: %s' % (self.lovd_genes_json))
		Utils.save_json_filenane(self.lovd_genes_json, self.lovd_transcript_dict)

	def _search_lovd(self, transcript, variation):

		if not transcript in self.lovd_transcript_dict:
			logging.warning('Transcript %s does not appear to be in LOVD' % (transcript))
			return None, None, None, None

		gene, genome = self.lovd_transcript_dict[transcript]
		lovd_gene_url = self.lovd_variants_url.format(gene=gene)
		lovd_gene_filename = os.path.join(self.lovd_directory, gene + '.atom')
		logging.info('LOVD entry for trascript %s is gene %s ' % (transcript, gene))
		logging.info('Looking for LOVD file: %s' % (lovd_gene_filename))
		if not Utils.file_exists(lovd_gene_filename):
			logging.info('Filename: %s does not exist . Downloading from: %s' % (lovd_gene_filename, lovd_gene_url))
			Utils.download(lovd_gene_url, lovd_gene_filename)
		else:
			logging.info('Filename: %s exists' % (lovd_gene_filename))

		logging.info('Parsing XML atom file: %s' % (lovd_gene_filename))
		data = feedparser.parse(lovd_gene_filename)


		for entry_index, entry in enumerate(data['entries']):
			#print entry.keys()
			#print entry['content']
			#print len(entry['content'])
			#print entry['content'][0].keys()
			#print entry['content'][0]['value']
			entry_value = entry['content'][0]['value']

			# Example: position_mRNA:NM_000367.2:c.*2240
			position_mRNA =  [''.join(x.split(':')[1:]) for x in entry_value.split('\n') if 'position_mRNA' in x][0]

			# Variant/DNA:c.*2240A>T
			variant_DNA = [x.split(':')[1] for x in entry_value.split('\n') if 'Variant/DNA' in x][0]

			# Match: 
			# position_genomic:chr6:18155397
			# position_genomic:chr6:18155437_18155384 
			search = re.search(r'position_genomic:chr([\w]+):([\w\?]+)|position_genomic:chr([\w]+):([\w]+)_([\w]+)', entry_value)
			if search is None:
				logging.warning('Filename: %s Could not find position_genomic in entry: %s' % (lovd_gene_filename, entry))
				continue

			#print position_mRNA, variant_DNA, position_genomic
			#print position_mRNA, variant_DNA, variation
			if variant_DNA == variation:
				logging.info('Found LOVD entry: \n%s' % entry_value)
				chrom = search.group(1)
				pos_1 = search.group(2)
				if pos_1 == '?':
					pos_1 = None
				else:
					pos_1 = int(pos_1)

				if len(search.groups()) == 4:
					pos_2 = int(search.group(3))
				else:
					pos_2 = None
				
				logging.info('Found: Chrom: %s  pos_1: %s  pos_2: %s Genome: %s' % (str(chrom), str(pos_1), str(pos_2), genome))
				return chrom, pos_1, pos_2, genome

		logging.error('Could not find %s:%s in file: %s' % (transcript, variation, lovd_gene_filename))
		return None, None, None, None

	def _search_mutalyzer(self, variant, gene=None, **kwargs):
		'''
		'''

		#Check if gene is defined
		if not gene is None:
			if not gene in variant:
				#we need to change the name of the variant.
				variant_splitted = variant.split(':')
				if len(variant_splitted) != 2:
					logging.error('More than one (or none) ":" characters detected in variant: %s' % (variant))
					return None
				new_variant = variant_splitted[0] + '(' + str(gene) + '):' + variant_splitted[1]
				logging.info('Mutalyzer. Changed variant name from %s to %s' % (variant, new_variant))
				variant = new_variant

		variant_url_encode = urllib.quote(variant)
		if '/' in variant_url_encode:
			logging.error('Variant: %s . Variant contains character: "/" . Aborting.. ' % (str(variant_url_encode)) )
			return None

		variant_filename = os.path.join(self.mutalyzer_directory, variant_url_encode + '.html')
		logging.info('Variant: %s . Mutalyzer variant filename: %s' % (variant, variant_filename))
		if not Utils.file_exists(variant_filename):
			logging.info('Variant: %s . Mutalyzer variant filename: %s does not exist. Creating it..' % (variant, variant_filename))
			variant_url = self.mutalyzer_url.format(variant=variant_url_encode)
			logging.info('Variant: %s . Variant Mutalyzer url: %s' % (variant, variant_url))
			Utils.download(variant_url, variant_filename)

			#Check for errors
			with open(variant_filename) as f:
				soup = BeautifulSoup(f)

			alert_danger = soup.find_all(class_="alert alert-danger")
			if len(alert_danger) > 0:
				logging.error('Variant: %s Mutalyzer returned the following critical error:' % (variant))
				logging.error(alert_danger[0].text)
				logging.error('Variant file will not be saved')
				os.remove(variant_filename)
				return None

		logging.info('Variant: %s . Mutalyzer file: %s exists (or created). Parsing..' % (variant, variant_filename))
		with open(variant_filename) as f:
			soup = BeautifulSoup(f)

		description = soup.find_all(class_='name-checker-left-column')[0].find_all('p')[0].text
		logging.info('Variant: %s . Found description: %s' % (variant, description))

		new_variant_url = soup.find_all(class_='name-checker-left-column')[0].find_all('p')[1].code.a.get('href')
		logging.info('Variant: %s . Found new variant url: %s' % (variant, new_variant_url))

		new_variant = new_variant_url.split('=')[1]
		new_variant = urllib.unquote(new_variant)
		logging.info('Variant: %s . Found Genomic description: %s' % (variant, new_variant))

		return new_variant

	def _search_ucsc(self, variant):
		'''
		Adapted from: https://www.biostars.org/p/59249/ 
		Variant should be an rs variant
		'''

		results = list(self.ucsc_dbsnp.filter_by(name=variant))
		logging.info('Variant: %s . Returned from UCSC filter_by: %s' % (str(variant), str(results)))

		ret = []

		for result in results:
			chrom = result.chrom
			start = result.chromStart  
			offset = result.chromEnd # This is the position reported from dbSNP
			refNCBI = result.refNCBI
			refUCSC = result.refUCSC

			reference = refNCBI
			if refNCBI != refUCSC:
				logging.warning('Variant: %s has different reference in NCBI (%s) and UCSC (%s)' % (variant, refNCBI, refUCSC))
				logging.warning('Keeping NCBI reference')
			
			observed = result.observed
			observed_s = observed.split('/')
			if result.strand == u'-':
				observed_s = list([MutationInfo.inverse(x) if not x in ['-'] else '-' for x in ''.join(observed_s)]) # Do not invert '-'
			alternative = [x for x in observed_s if x != reference]
			logging.info('Variant: %s . observed: %s alternate: %s' % (variant, observed, str(alternative)))
			if len(alternative) == 1:
				alternative = alternative[0]


			ret.append(self._build_ret_dict(chrom, offset, reference, alternative, self.genome, 'UCSC'))

		if len(ret) == 1:
			return ret[0]

		return ret

	def _search_VEP(self, variant):
		'''
		Variant Effect Predictor
		'''

		v = VEP(variant)
		if not type(v) is list:
			logging.error('Variant: %s . VEP did not return a list!' % (variant))
			return None

		if len(v) == 0:
			logging.error('Variant: %s . VEP returned an empty list' % (variant))
			return None

		logging.info('Variant: %s . VEP returned %i results. Getting the info from the first' % (variant, len(v)))

		allele_string = v[0]['allele_string']
		logging.info('Variant: %s . Allele string: %s' % (variant, allele_string))
		allele_string_s = allele_string.split('/')

		# Looking for 'transcript_consequences'
		variant_alleles = []
		if 'transcript_consequences' in v[0]:
			# Getting all variant alleles
			for t_c in v[0]['transcript_consequences']:
				if 'variant_allele' in t_c:
					variant_alleles.append(t_c['variant_allele'])

		#Get all different variant alleles
		variant_alleles = list(set(variant_alleles))

		if len(variant_alleles) > 1:
			logging.warning('Variant: %s . More than one variant alleles found' % (variant))
		if len(variant_alleles) == 0:
			logging.warning('Variant: %s . No variant alleles found' % (variant))

		reference = [x for x in allele_string_s if x not in variant_alleles]
		if len(reference) == 1:
			reference = reference[0]
		elif len(reference) == 0:
			reference = ''

		if len(variant_alleles) == 1:
			variant_alleles = variant_alleles[0]

		arguments = [
			v[0]['seq_region_name'], # chrom
			v[0]['start'], # offset
			reference, # ref
			variant_alleles, # alt
			v[0]['assembly_name'], # genome
			'VEP', # source
		]

		return self._build_ret_dict(*arguments)

	def _build_ret_dict(self, *args):
		return {
			'chrom' : args[0],
			'offset' : args[1],
			'ref' : args[2],
			'alt' : args[3],
			'genome' : args[4],
			'source' : args[5],
		}



class Counsyl_HGVS(object):
	'''
	Wrapper class for pyhgvs https://github.com/counsyl/hgvs 
	'''

	fasta_url_pattern = 'http://hgdownload.cse.ucsc.edu/goldenPath/{genome}/bigZips/chromFa.tar.gz'
	refseq_url = 'https://github.com/counsyl/hgvs/raw/master/pyhgvs/data/genes.refGene'

	def __init__(self, local_directory, genome='hg19'):

		self.local_directory = local_directory
		self.genome = genome

		# Check genome option
		if re.match(r'hg[\d]+', genome) is None:
			raise ValueError('Parameter genome should follow the pattern: hgDD (for example hg18, hg19, hg38) ')

		#Init counsyl PYHGVS
		self.fasta_directory = os.path.join(self.local_directory, genome)
		self.fasta_filename = os.path.join(self.fasta_directory, genome + '.fa')
		self.refseq_filename = os.path.join(self.local_directory, 'genes.refGene')
		if not Utils.file_exists(self.fasta_filename):
			logging.info('Could not find fasta filename: %s' % self.fasta_filename)
			self._install_fasta_files()
		else:
			logging.info('Found fasta filename: %s' % self.fasta_filename)

		self.sequence_genome = SequenceFileDB(self.fasta_filename)
		self._load_transcripts()

	def hgvs_to_vcf(self, variant):
		chrom, offset, ref, alt = hgvs_counsyl.parse_hgvs_name(
			variant, self.sequence_genome, get_transcript=self._get_transcript)

		return chrom, offset, ref, alt


	def _load_transcripts(self):
		logging.info('Indexing transcripts..')
		with open(self.refseq_filename) as f:
			self.transcripts = hgvs_counsyl_utils.read_transcripts(f)

	def _get_transcript(self, name):
			return self.transcripts.get(name)

	def _install_fasta_files(self):
		fasta_filename_tar_gz = os.path.join(self.fasta_directory, 'chromFa.tar.gz')
		fasta_filename_tar = os.path.join(self.fasta_directory, 'chromFa.tar')
		fasta_url = self.fasta_url_pattern.format(genome=self.genome)
		logging.info('Downloading from: %s' % fasta_url)
		logging.info('Downloading to: %s' % fasta_filename_tar_gz)

		Utils.mkdir_p(self.fasta_directory)
		Utils.download(fasta_url, fasta_filename_tar_gz)

		logging.info('Unzipping to: %s' % fasta_filename_tar)
		Utils.gunzip(fasta_filename_tar_gz, fasta_filename_tar)

		logging.info('Untar to: %s' % self.fasta_directory)
		Utils.untar(fasta_filename_tar, self.fasta_directory)

		logging.info('Merging *.fa to %s.fa' % (self.genome))
		all_fasta_filenames_glob = os.path.join(self.fasta_directory, 'chr*.fa')
		all_fasta_filenames = glob.glob(all_fasta_filenames_glob)
		Utils.cat_filenames(all_fasta_filenames, self.fasta_filename)

		logging.info('Downloading refGene')
		logging.info('Downloading from: %s' % self.refseq_url)
		logging.info('Downloading to: %s' % self.refseq_filename)
		Utils.download(self.refseq_url, self.refseq_filename)


class Utils(object):
	'''
	Useful functions to help manange files
	'''

	@staticmethod
	def directory_exists(dirname):
		'''
		Check if directory exists
		'''
		return os.path.isdir(dirname)

	@staticmethod
	def file_exists(filename):
		'''
		Check if filename exists
		'''
		return os.path.isfile(filename) 


	@staticmethod
	def mkdir_p(dirname):
		'''
		Create directory
		Functionality similar with: mkdir -p
		Reference: http://stackoverflow.com/questions/600268/mkdir-p-functionality-in-python 
		'''
		try:
			os.makedirs(dirname)
		except OSError as exc: # Python >2.5
			if exc.errno == errno.EEXIST and os.path.isdir(dirname):
				pass
			else:
				raise

	@staticmethod
	def load_json_filename(filename):
		'''
		Load a json file
		'''
		with open(filename) as f:
			data = json.load(f)

		return data

	@staticmethod
	def save_json_filenane(filename, data):
		'''
		Save a json file
		'''
		with open(filename, 'w') as f:
			f.write(json.dumps(data, indent=4) + '\n')

	@staticmethod
	def download(url, filename=None):
		'''
		http://www.pypedia.com/index.php/download
		'''
		if not filename:
			file_name = url.split('/')[-1]
		else:
			file_name = filename
			
		u = urllib2.urlopen(url)
		f = open(file_name, 'wb')
		meta = u.info()
		try:
			file_size = int(meta.getheaders("Content-Length")[0])
			pb = ProgressBar(file_size, 'Progress')
		except IndexError:
			file_size = None
			logging.warning('Could not determine file size')
		print("Downloading: {0} Bytes: {1}".format(url, file_size))

		file_size_dl = 0
		block_sz = 8192
		while True:
			buffer = u.read(block_sz)
			if not buffer:
				break

			file_size_dl += len(buffer)
			f.write(buffer)
			if file_size:
				pb.animate_ipython(file_size_dl)
		print # We need a new line here
		f.close()

	@staticmethod
	def gunzip(compressed_filename, uncompressed_filename):
		'''
		unzips a gunzip file
		https://docs.python.org/2/library/gzip.html
		'''

		with gzip.open(compressed_filename, 'rb') as f_out, open(uncompressed_filename, 'wb') as f_in:
			shutil.copyfileobj(f_out, f_in)

	@staticmethod
	def untar(tar_filename, path):
		'''
		Untar a filename
		https://docs.python.org/2/library/tarfile.html
		'''

		with tarfile.open(tar_filename) as tar:
			tar.extractall(path=path)

	@staticmethod
	def cat_filenames(filenames, output_filename):
		'''
		Concat filenames
		http://stackoverflow.com/questions/13613336/python-concatenate-text-files 
		'''

		with open(output_filename, 'w') as outfile:
			for fname in filenames:
				logging.info('Concatenating: %s' % fname)
				with open(fname) as infile:
					for line in infile:
						outfile.write(line)

	@staticmethod
	def get_application_dir(application_name):
		'''
		Create a cross platform local directory for this app
		Reference: https://pypi.python.org/pypi/appdirs/1.4.0 
		'''
		directory = user_data_dir(application_name, '')
		if not Utils.directory_exists(directory):
			Utils.mkdir_p(directory)

		return directory


class ProgressBar:
	'''
	http://www.pypedia.com/index.php/ProgressBar
	'''
	def __init__(self, iterations, msg = ''):
		self.iterations = iterations
		self.prog_bar = '[]'
		self.msg = msg
		self.fill_char = '*'
		self.width = 40
		self.__update_amount(0)
		if have_ipython:
			self.animate = self.animate_ipython
		else:
			self.animate = self.animate_noipython

	def animate_ipython(self, iter):
		try:
			clear_output()
		except Exception:
			# terminal IPython has no clear_output
			pass
		print '\r', self,
		sys.stdout.flush()
		self.update_iteration(iter + 1)

	def animate_noipython( self, iter ):
		'''
		https://github.com/tomevans/pyhm/blob/master/pyhm/ProgressBar.py
		'''
		if sys.platform.lower().startswith( 'win' ):
			print self, '\r',
		else:
			print self, chr( 27 ) + '[A'
		self.update_iteration( iter )
		# time.sleep( 0.5 )


	def update_iteration(self, elapsed_iter):
		self.__update_amount((elapsed_iter / float(self.iterations)) * 100.0)
		self.prog_bar += '  %d of %s complete' % (elapsed_iter, self.iterations)

	def __update_amount(self, new_amount):
		percent_done = int(round((new_amount / 100.0) * 100.0))
		all_full = self.width - 2
		num_hashes = int(round((percent_done / 100.0) * all_full))
		self.prog_bar = self.msg + '[' + self.fill_char * num_hashes + ' ' * (all_full - num_hashes) + ']'
		pct_place = (len(self.prog_bar) / 2) - len(str(percent_done))
		pct_string = '%d%%' % percent_done
		self.prog_bar = self.prog_bar[0:pct_place] +             (pct_string + self.prog_bar[pct_place + len(pct_string):])

	def __str__(self):
		return str(self.prog_bar)

def test():
	'''
	Testing cases
	'''

	logging.basicConfig(level=logging.INFO)

	print '------FUZZY HGVS CORRECTOR---------'
	print MutationInfo.fuzzy_hgvs_corrector('1048G->C')
	print MutationInfo.fuzzy_hgvs_corrector('1048G->C', transcript='NM_001042351.1')
	try: 
		MutationInfo.fuzzy_hgvs_corrector('1048G->C', transcript='NM_001042351.1', ref_type='p')
	except Exception as e:
		print 'Exception:', str(e)
	print MutationInfo.fuzzy_hgvs_corrector('1048G->C', transcript='NM_001042351.1', ref_type='c')

	print MutationInfo.fuzzy_hgvs_corrector('1387C->T/A', transcript='NM_001042351.1', ref_type='c')
	print MutationInfo.fuzzy_hgvs_corrector('1387C->T/A')

	print MutationInfo.fuzzy_hgvs_corrector('-1923(A>C)', transcript='NT_005120.15', ref_type='g')

	print MutationInfo.fuzzy_hgvs_corrector('NT_005120.15:c.1160(CC>GT)')

	print '--------HGVS PARSER-----------------'
	print MutationInfo.biocommons_parse('unparsable')

	mi = MutationInfo()
	print mi.get_info('rs305974')
	a=1/0
	
	print '-------Mutalyzer---------------------'
	print mi._search_mutalyzer('NT_005120.15:c.IVS1-72T>G', gene='UGT1A1')

	print '--------LOVD------------------------'
	print mi.lovd_transcript_dict['NM_000367.2']
	chrom, pos_1, pos_2, genome = mi._search_lovd('NM_000367.2', 'c.-178C>T')

	print '--------GET INFO--------------------'

	print mi.get_info('1387C->T/A', transcript='NM_001042351.1', ref_type='c') # This should try first to correct 
	print mi.get_info('1387C->T/A') # This should fail (return None)
	print mi.get_info('NM_006446.4:c.1198T>G')
	#print mi.get_info('XYZ_006446.4:c.1198T>G')
	#print mi.get_info('NM_006446.4:c.456345635T>G')
	print mi.get_info('NG_000004.3:g.253133T>C')
	print mi.get_info({})
	print mi.get_info(['NM_001042351.1:c.1387C>T', 'NM_001042351.1:c.1387C>A'])
	print mi.get_info('NC_000001.11:g.97593343C>A')
	print mi.get_info('M61857.1:c.121A>G')
	print mi.get_info('AY545216.1:g.8326_8334dupGTGCCCACT')
	print mi.get_info('NT_005120.15:c.-1126C>T', gene='UGT1A1')
	print mi.get_info('NM_000367.2:c.-178C>T')
	print mi.get_info('NT_005120.15:c.IVS1-72T>G', gene='UGT1A1')

	# Testing rs SNPs
	print mi.get_info('rs53576')
	print mi.get_info('rs4646438') # insertion variation 
	print mi.get_info('rs305974') # This SNP is not in UCSC 


	print '=' * 20
	print 'TESTS FINISHED'








import os
import re
import sys
import glob
import gzip
import json
import time
import errno
import shutil
import logging
import tarfile 
import urllib2
import requests

from Bio import Entrez
from appdirs import *

# Importing: https://bitbucket.org/biocommons/hgvs
import hgvs as hgvs_biocommons
import hgvs.parser as hgvs_biocommons_parser

# Importing https://github.com/counsyl/hgvs 
# How to setup data files : https://github.com/counsyl/hgvs/blob/master/examples/example1.py 
import pyhgvs as hgvs_counsyl
import pyhgvs.utils as hgvs_counsyl_utils
from pygr.seqdb import SequenceFileDB
# Use this package to retrieve genomic position for known refSeq entries.
# MutationInfo comes to the rescue when pyhgvs fails

from bs4 import BeautifulSoup 

# For progress bar..
try:
	from IPython.core.display import clear_output
	have_ipython = True
except ImportError:
	have_ipython = False


__docformat__ = 'reStructuredText'

"""
TODO: 
* More documentation   http://thomas-cokelaer.info/tutorials/sphinx/docstring_python.html 
* Fix setup.py http://stackoverflow.com/questions/3472430/how-can-i-make-setuptools-install-a-package-thats-not-on-pypi 
* Add hgvs_counsyl installation and automate these steps: https://github.com/counsyl/hgvs/blob/master/examples/example1.py  
* Maybe import inline for performance reasons? http://stackoverflow.com/questions/477096/python-import-coding-style 
"""

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

	def __init__(self, local_directory=None, email=None, genome='hg19'):

		#Check genome value
		match = re.match(r'hg[\d]+', genome)
		if not match:
			raise ValueError('genome parameter should be hgDD (for example hg18, hg19, hg38, ...)')
		self.genome = genome

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
			logging.warning('Could not parse variant:  %s . Error: %s' % (str(variant), str(e)))
			return None

	@staticmethod
	def fuzzy_hgvs_corrector(variant, transcript=None, ref_type=None):
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
			logging.warning('Variant: %s  . "/" found suggesting that this contains 2 variants' % (new_variant))
			new_variant_1 = re.sub(r'([ACGT])>([ACGT])/([ACGT])', r'\1>\2', new_variant)
			new_variant_2 = re.sub(r'([ACGT])>([ACGT])/([ACGT])', r'\1>\3', new_variant)
			return [
				MutationInfo.fuzzy_hgvs_corrector(new_variant_1), 
				MutationInfo.fuzzy_hgvs_corrector(new_variant_2)]

		# Case 3
		# -1126(C>T) 
		# The variant contains parenthesis in the substitition
		search = re.search(r'[\d]+\([ACGT]>[ACGT]\)', new_variant)
		if search:
			logging.warning('Variant: %s   . Contains parenthesis around substitition. Removing the parenthesis' % (new_variant))
			new_variant = re.sub(r'([\d]+)\(([ACGT])>([ACGT])\)', r'\1\2>\3', new_variant)

		return new_variant

	def _get_info_rs(self, variant):
		logging.error('Variant: %s Sorry.. info for rs variants are not yet implemented' % (variant))
		return None

	def get_info(self, variant, **kwargs):
		"""
		Doing our best to get the most out of a variant name

		:param variant: A variant

		"""

		def build_ret_dict(*args):
			return {
				'chrom' : args[0],
				'offset' : args[1],
				'ref' : args[2],
				'alt' : args[3],
				'genome' : args[4]
			}

		#Is this an rs variant?
		match = re.match(r'rs[\d]+', variant)
		if match:
			# This is an rs variant 
			return self._get_info_rs()

		#Is this an hgvs variant?
		hgvs = MutationInfo.biocommons_parse(variant)
		if hgvs is None:
			#Parsing failed. Trying to fix possible problems
			new_variant = MutationInfo.fuzzy_hgvs_corrector(variant, **kwargs)
			if type(new_variant) is list:
				return [get_info(v) for v in new_variant]
			elif type(new_variant) is str:
				hgvs = MutationInfo.biocommons_parse(new_variant)

		if hgvs is None:
			#Parsing failed again.. Nothing to do..
			logging.error('Failed to parse variant: %s . Returning None' % (variant))
			return None

		#Up to here we have managed to parse the variant
		hgvs_transcript = hgvs.ac
		hgvs_type = hgvs.type
		hgvs_position = hgvs.posedit.pos.start.base
		hgvs_reference = hgvs.posedit.edit.ref

		#Converting the variant to VCF 
		#Try pyhgvs 
		try:
			chrom, offset, ref, alt = self.counsyl_hgvs.hgvs_to_vcf(variant)
			return build_ret_dict(chrom, offset, ref, alt, self.genome)
		except KeyError as e:
			logging.warning('Variant: %s . pyhgvs KeyError: %s' % (variant, str(e)))
		except ValueError as e:
			logging.warning('Variant: %s . pyhgvs ValueError: %s' % (variant, str(e)))
		except IndexError as e:
			logging.warning('Variant: %s . pyhgvs IndexError: %s' % (variant, str(e)))

		logging.info('pyhgvs failed...')
		logging.info('Fetching fasta sequence for trascript: %s' % (hgvs_transcript))
		fasta = self._get_fasta_from_nucleotide_entrez(hgvs_transcript)

		# Check variant type
		if hgvs_type == 'c':
			logging.warning('Variant: %s . ***SERIOUS** This is a c (coding DNA) variant. Assuming continuous coding positions.')
		elif hgvs_type == 'g':
			#This should be fine
			pass
		else:
			logging.error('Variant: %s Sorry.. only c (coding DNA) and g (genomic) variants are supported so far.' % (variant))
			return None

		logging.info('Variant: %s . Reference on fasta: %s  Reference on variant: %s' % (variant, fasta[hgvs_position-1], hgvs_reference))
		if fasta[hgvs_position-1] != hgvs_reference:
			logging.error('Variant: %s . ***SERIOUS*** Reference on fasta and Reference on variant name are different!' % (variant))

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
		logging.info('Variant: %s . blat results filename: %s' % (variant, blat_filename) )
		if not Utils.file_exists(blat_filename):
			logging.info('Variant: %s . blat filename does not exist. Requesting it from UCSC..')
			self._perform_blat(fasta_chunk, blat_filename)

		logging.info('Variant: %s . blat filename exists (or created). Parsing it..')
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
			logging.info('Variant: %s . Blat alignment filename soes not exist. Creating it..' % (variant))
			blat_temp_alignment_filename = blat_alignment_filename + '.tmp'
			logging.info('Variant: %s . Temporary blat alignment filename:' % (variant, blat_temp_alignment_filename))
			logging.info('Variant: %s . Downloading Details url in Temporary blat alignment filename' % (variant))
			Utils.download(details_url, blat_temp_alignment_filename)
			logging.info('Variant: %s . Parsing temporary blat alignment filename')
			with open(blat_temp_alignment_filename) as blat_temp_alignment_file:
				blat_temp_alignment_soup =  BeautifulSoup(blat_temp_alignment_soup)
			blat_real_alignment_url = 'https://genome.ucsc.edu/' + blat_temp_alignment_soup.find_all('frame')[1]['src'].replace('../', '')
			logging.info('Variant: %s . Real blat alignment URL: %s' % (variant, blat_real_alignment_url))
			blat_real_alignment_filename = blat_alignment_filename + '.html'
			logging.info('Variant: %s . Real blat alignment filename: %s' % (variant, blat_real_alignment_url))
			logging.info('Variant: %s . Downloading real blat alignment filename..')
			Utils.download(blat_real_alignment_url, blat_real_alignment_filename)
			logging.info('Variant: %s . Reading content from real alignment filename' % (variant))
			with open(blat_real_alignment_filename) as blat_real_alignment_file:
				# We have to define html.parser otherwise parsing is incomplete
				blat_real_alignment_soup = BeautifulSoup(blat_real_alignment_file, parser='html.parser')
				#Take the complete text
				blat_real_alignment_text = blat_real_alignment_soup.text

			logging.info('Variant: %s . Saving content to blat alignment filename: %s' % (variant, blat_alignment_filename))
			with open(blat_alignment_filename, 'w') as blat_alignment_file:
				blat_alignment_file.write(blat_real_alignment_text)

		logging.info('Variant: %s . blat alignment filename exists (or created)' % (variant))

	def _create_blat_filename(self, transcript, chunk_start, chunk_end):
		return os.path.join(self.blat_directory, 
			transcript + '_' + str(chunk_start) + '_' + str(chunk_end) + '.blat.results.html')

	def _create_blat_alignment_filename(self, transcript, chunk_start, chunk_end):
		return os.path.join(self.blat_directory, 
			transcript + '_' + str(chunk_start) + '_' + str(chunk_end) + '.blat')		

	def _get_fasta_from_nucleotide_entrez(self, ncbi_access_id): # For example NG_000004.3
		'''
		handle4 = Entrez.efetch(db='nucleotide', id='101011606', rettype='fasta', retmode='text') 

		'''

		def strip_fasta(fasta):
			'''
			Get a fasta file and removes "new line" and comments at the start
			'''
			return ''.join([x for x in fasta.split('\n') if '>' not in x])

		fasta = self._load_ncbi_fasta_filename(ncbi_access_id)
		if fasta is None:

			logging.info('Could not find local file for %s Querying Entrez..' % (ncbi_access_id))
			handle = Entrez.efetch(db='nuccore', id=ncbi_access_id, retmode='text', rettype='fasta')
			fasta = handle.read()

			logging.info('Fasta fetched: %s...' % (fasta[0:20]))
		
			self._save_ncbi_fasta_filename(ncbi_access_id, fasta)

		return strip_fasta(fasta)


	def _ncbi_fasta_filename(self, ncbi_access_id):
		'''
		Create filename that contains NCBI fasta file
		'''
		return os.path.join(self.transcripts_directory, ncbi_access_id + '.fasta')

	def _save_ncbi_fasta_filename(self, ncbi_access_id, fasta):
		'''
		Save NCBI fasta to file
		'''

		filename = self._ncbi_fasta_filename(ncbi_access_id)
		if not Utils.file_exists(filename):
			with open(filename, 'w') as f:
				f.write(fasta)

	def _load_ncbi_fasta_filename(self, ncbi_access_id):
		'''
		Load NCBI fasta file
		'''
		filename = self._ncbi_fasta_filename(ncbi_access_id)
		if not Utils.file_exists(filename):
			return None

		logging.info('Found trascript fasta filename: %s' % (filename))
		with open(filename) as f:
			fasta = f.read()

		return fasta

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

		with open(output_filename) as f:
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
			soup = BeautifulSoup(f)

		header = bs.soup.find_all('pre')[0].text.split('\n')[0].split()[1:]
		header[header.index('START')] = 'RELATIVE_START'
		header[header.index('END')] = 'RELATIVE_END'

		all_urls = [x.get('href') for x in bs.soup.find_all('pre')[0].find_all('a')]
		all_urls_pairs = zip(all_urls[::2], all_urls[1::2])

		ret = [{k:v for k,v in zip(header, x.split()[2:])} for x in bs.soup.find_all('pre')[0].text.split('\n') if 'details' in x]

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
			raise ValueError('Parameter genome should follow the patern: hgDD (for example hg18, hg19, hg38) ')

		#Init counsyl PYHGVS
		self.fasta_directory = os.path.join(self.local_directory, genome)
		self.fasta_filename = os.path.join(self.fasta_directory, genome + '.fa')
		self.refseq_filename = os.path.join(self.local_directory, 'genes.refGene')
		if not Utils.file_exists(self.fasta_filename):
			logging.info('Could not find fasta filename: %s' % self.fasta_filename)
			_install_fasta_files()
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
		fasta_filename_tar_gz = os.path.join(fasta_directory, 'chromFa.tar.gz')
		fasta_filename_tar = os.path.join(fasta_directory, 'chromFa.tar')
		fasta_url = self.fasta_url_pattern.format(genome=self.genome)
		logging.info('Downloading from: %s' % fasta_url)
		logging.info('Downloading to: %s' % fasta_filename_tar_gz)

		Utils.mkdir_p(fasta_directory)
		Utils.download(fasta_url, fasta_filename_tar_gz)

		logging.info('Unzipping to: %s' % fasta_filename_tar)
		Utils.gunzip(fasta_filename_tar_gz, fasta_filename_tar)

		logging.info('Untar to: %s' % fasta_directory)
		Utils.untar(fasta_filename_tar, fasta_directory)

		logging.info('Merging *.fa to %s.fa' % (self.genome))
		all_fasta_filenames_glob = os.path.join(fasta_directory, 'chr*.fa')
		all_fasta_filenames = glob.glob(all_fasta_filenames_glob)
		Utils.cat_filenames(all_fasta_filenames, fasta_filename)

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

	print MutationInfo.fuzzy_hgvs_corrector('-1923(A>C)', transcript='NT_005120.15', ref_type='g')

	print '--------HGVS PARSER-----------------'
	print MutationInfo.biocommons_parse('unparsable')

	print '--------GET INFO--------------------'
	mi = MutationInfo()
	print mi.get_info('NM_006446.4:c.1198T>G')
	#print mi.get_info('XYZ_006446.4:c.1198T>G')
	#print mi.get_info('NM_006446.4:c.456345635T>G')
	print mi.get_info('NG_000004.3:g.253133T>C')

	print 'TESTS FINISHED'
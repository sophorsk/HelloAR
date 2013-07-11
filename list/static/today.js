function getToday(url) {
        $.ajax({
	    url: url,
	    success: function(list){
		$("#today").html(list);
	    }
	});
}

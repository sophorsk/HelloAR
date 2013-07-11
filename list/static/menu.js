window.onload=function()
{
	setPage();
}

function setActiveMenu(arr, crtPage)
{
    for (var i=0; i < arr.length; i++)
    {
	if (arr[i].href == crtPage)
	{
	    if (arr[i].parentNode.tagName != "DIV")
	    {
		arr[i].className = "current";
	    }
	}
    }
}

function setPage()
{
    hrefString = document.location.href ? document.location.href : document.location;
    
    if (document.getElementById("menu") !=null )
	setActiveMenu(document.getElementById("menu").getElementsByTagName("a"), hrefString);
}
